import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, AsyncMock

from ev.daily import DailyStore
from ev.process_watches import ProcessWatches
from ev.tools.process_lifetime import process_lifetime


class ProcessWatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DailyStore(Path(self.temp.name) / "daily.db")
        self.now = 100
        observation = process_lifetime({"pid": os.getpid()}, None)
        self.identity = {k: observation[k] for k in ("pid", "start_ticks", "boot_id")}
        self.observe = Mock(return_value=observation)
        self.watches = ProcessWatches(self.store, observe=self.observe, clock=lambda: self.now)

    def test_watch_restart_resumes_observation_without_replaying_actions(self):
        watch = self.watches.create(self.identity)
        restarted = ProcessWatches(
            DailyStore(self.store.path), observe=self.observe, clock=lambda: self.now
        )
        self.assertEqual(restarted.list()[0]["id"], watch["id"])
        self.assertEqual(restarted.tick(), [])
        self.observe.return_value = {
            **self.observe.return_value,
            "lifetime_status": "ABSENT",
            "start_ticks": None,
        }
        ended = restarted.tick()[0]
        self.assertEqual(ended["status"], "LIFETIME_ENDED")
        self.assertFalse(ended["task_success_verified"])
        self.assertFalse(ended["actions_replayed"])
        self.assertFalse(ended["exit_code_known"])
        self.observe.reset_mock()
        self.assertEqual(restarted.tick(), [])
        self.observe.assert_not_called()

    def test_registration_is_idempotent_and_does_not_extend_deadline(self):
        first = self.watches.create(self.identity, 10)
        self.now += 3
        self.assertEqual(self.watches.create(self.identity, 100), first)
        self.assertEqual(len(self.watches.list()), 1)

    def test_expired_registration_cannot_claim_a_new_active_watch_before_polling(self):
        first = self.watches.create(self.identity, 10)
        self.now += 10
        with self.assertRaisesRegex(ValueError, "deadline has expired"):
            self.watches.create(self.identity, 100)
        self.assertEqual(self.watches.list()[0]["deadline"], first["deadline"])
        self.assertEqual(self.watches.tick()[0]["status"], "TIMED_OUT")
        self.assertNotEqual(self.watches.create(self.identity, 100)["id"], first["id"])

    def test_reboot_deadline_and_clock_rollback_do_not_claim_completion(self):
        for mode, expected in (
            ("reboot", "IDENTITY_CHANGED"),
            ("timeout", "TIMED_OUT"),
            ("rollback", "CLOCK_CHANGED"),
        ):
            with self.subTest(mode=mode):
                self.now = 100
                self.observe.return_value = {**self.identity, "lifetime_status": "RUNNING"}
                watch = self.watches.create(self.identity, 10)
                if mode == "reboot":
                    self.observe.return_value = {
                        **self.identity,
                        "boot_id": "different",
                        "lifetime_status": "ABSENT",
                    }
                else:
                    self.now = 110 if mode == "timeout" else 90
                self.assertEqual(self.watches.tick()[0]["status"], expected)

    def test_cancel_does_not_signal_or_keep_polling_target(self):
        watch = self.watches.create(self.identity)
        self.assertEqual(self.watches.cancel(watch["id"])["processes_signalled"], 0)
        self.observe.reset_mock()
        self.assertEqual(self.watches.tick(), [])
        self.observe.assert_not_called()

    def test_invalid_targets_or_deadlines_rejected_before_observation(self):
        for args in (
            {"identity": {"pid": 1}},
            {"identity": self.identity, "timeout": float("nan")},
            {"identity": self.identity, "timeout": 86401},
        ):
            with self.assertRaises(ValueError):
                self.watches.create(**args)
        self.observe.assert_not_called()
        self.assertEqual(self.watches.list(), [])

    def test_tampered_persisted_identity_fails_without_observation(self):
        watch = self.watches.create(self.identity)
        watch["identity"]["pid"] = "bad"
        self.store.save("process_watch", watch["id"], watch)
        self.observe.reset_mock()
        self.assertEqual(self.watches.tick()[0]["status"], "OBSERVATION_FAILED")
        self.observe.assert_not_called()

    def test_active_limit_and_cancel_all_leave_processes_alone(self):
        self.observe.side_effect = lambda args, context: {
            **self.identity,
            **args,
            "lifetime_status": "RUNNING",
        }
        for number in range(8):
            self.watches.create({**self.identity, "pid": 100 + number})
        with self.assertRaisesRegex(ValueError, "Eight"):
            self.watches.create({**self.identity, "pid": 200})
        self.assertEqual(len(self.watches.cancel()["cancelled_watch_ids"]), 8)
        self.observe.reset_mock()
        self.assertEqual(self.watches.tick(), [])
        self.observe.assert_not_called()


class ProcessWatchIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_worker_observes_real_child_and_emits_event_without_model_calls(self):
        from ev.paths import Paths
        from ev.service import CarlosCore

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = CarlosCore(
                paths=Paths(*(root / n for n in ("config", "data", "state", "cache", "runtime")))
            )
            service.brain.submit = AsyncMock(side_effect=AssertionError("No model callback"))
            child = await asyncio.create_subprocess_exec(
                sys.executable, "-c", "import time; time.sleep(.7)"
            )
            worker = None
            try:
                observation = process_lifetime({"pid": child.pid}, None)
                identity = {k: observation[k] for k in ("pid", "start_ticks", "boot_id")}
                registered = await service.request_tool(
                    {"name": "agent.watch_process", "arguments": identity}, "watch-fixture"
                )
                self.assertTrue(registered["result"]["verified"])
                worker = asyncio.create_task(service._process_watch_loop())
                await child.wait()
                async with asyncio.timeout(5):
                    while not any(
                        e["type"] == "task.process_watch_changed" for e in service.bus.history()
                    ):
                        await asyncio.sleep(0.1)
                events = [
                    e for e in service.bus.history() if e["type"] == "task.process_watch_changed"
                ]
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["payload"]["status"], "LIFETIME_ENDED")
                service.brain.submit.assert_not_awaited()
            finally:
                if worker:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
                await child.wait()
                service.memory.close()
