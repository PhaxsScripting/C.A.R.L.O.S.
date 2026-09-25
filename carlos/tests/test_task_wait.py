import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from ev.task_wait import wait_for_conditions
from ev.paths import Paths
from ev.service import CarlosCore

CONDITIONS = [{"kind": "audio_muted", "expected": True}]


class WaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_observes_changed_state_without_replaying_actions(self):
        request = AsyncMock(
            side_effect=[
                {"status": "completed", "result": {"muted": False}},
                {"status": "completed", "result": {"muted": True}},
            ]
        )
        events = Mock()
        result = await wait_for_conditions(
            CONDITIONS, request, "test", timeout=2, interval=0.5, notify=events
        )
        self.assertTrue(result["verified"])
        self.assertEqual(result["polls"], 2)
        self.assertEqual(result["progress_changes"], 1)
        self.assertTrue(
            all(c.args[0]["name"] == "audio.get_volume" for c in request.await_args_list)
        )
        self.assertEqual(events.call_args.args[0], "WAIT_FINISHED")

    async def test_timeout_does_not_claim_success(self):
        request = AsyncMock(return_value={"status": "completed", "result": {"muted": False}})
        result = await wait_for_conditions(CONDITIONS, request, None, timeout=1, interval=0.5)
        self.assertFalse(result["ok"])
        self.assertEqual(result["wait_state"], "TIMED_OUT")

    async def test_cancellation_interrupts_wait_and_emits_terminal_event(self):
        request = AsyncMock(return_value={"status": "completed", "result": {"muted": False}})
        events = Mock()
        task = asyncio.create_task(
            wait_for_conditions(CONDITIONS, request, None, timeout=1800, notify=events)
        )
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(request.await_count, 1)
        self.assertEqual(events.call_args.args[0], "WAIT_FINISHED")

    async def test_invalid_wait_limits_rejected_before_observation(self):
        for timeout in (0, 1801, float("nan"), True):
            with self.assertRaises(ValueError):
                await wait_for_conditions(CONDITIONS, AsyncMock(), None, timeout=timeout)

    async def test_wait_uses_real_service_executor_without_planner_lock_recursion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = CarlosCore(
                paths=Paths(*(root / n for n in ("config", "data", "state", "cache", "runtime")))
            )
            try:
                service.tools.get("audio.get_volume").executor = lambda a, c: {"muted": True}
                async with asyncio.timeout(2):
                    result = await service._request_model_tool(
                        {"name": "agent.wait_for", "arguments": {"conditions": CONDITIONS}},
                        "wait-test",
                    )
                self.assertEqual(result["status"], "completed", result)
                self.assertTrue(result["result"]["verified"])
            finally:
                service.memory.close()

    async def test_wait_does_not_consume_reasoning_budget_but_wall_time_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = CarlosCore(
                paths=Paths(*(root / n for n in ("config", "data", "state", "cache", "runtime")))
            )
            try:
                brain = service.brain
                brain._wait_seconds["task"] = 300
                with patch("ev.brain.time.monotonic", return_value=400):
                    self.assertFalse(brain._budget_elapsed("task", 0))
                    self.assertTrue(brain._budget_elapsed("other", 0))
                with patch("ev.brain.time.monotonic", return_value=3601):
                    self.assertTrue(brain._budget_elapsed("task", 0))
            finally:
                service.memory.close()

    async def test_planner_cancel_interrupts_long_poll_interval_promptly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = CarlosCore(
                paths=Paths(*(root / n for n in ("config", "data", "state", "cache", "runtime")))
            )
            try:
                service.tools.get("audio.get_volume").executor = lambda a, c: {"muted": False}
                task = asyncio.create_task(
                    service._request_model_tool(
                        {
                            "name": "agent.wait_for",
                            "arguments": {
                                "conditions": CONDITIONS,
                                "timeout_seconds": 1800,
                                "interval_seconds": 60,
                            },
                        },
                        "cancel-wait",
                    )
                )
                for _ in range(100):
                    if service.planner.active:
                        break
                    await asyncio.sleep(0.01)
                service.planner.cancel("test")
                result = await asyncio.wait_for(task, 1)
                self.assertEqual(result["status"], "cancelled", result)
            finally:
                service.memory.close()

    async def test_exception_and_normal_return_release_wait_accounting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = CarlosCore(
                paths=Paths(*(root / n for n in ("config", "data", "state", "cache", "runtime")))
            )
            try:
                brain = service.brain
                async with brain._recover_failed_command("normal"):
                    brain._wait_seconds["normal"] = 90
                self.assertNotIn("normal", brain._wait_seconds)
                with self.assertRaises(asyncio.CancelledError):
                    async with brain._recover_failed_command("cancel"):
                        brain._wait_seconds["cancel"] = 90
                        raise asyncio.CancelledError()
                self.assertNotIn("cancel", brain._wait_seconds)
            finally:
                service.memory.close()

    async def test_direct_wait_honors_global_stop_generation_without_active_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = CarlosCore(
                paths=Paths(*(root / n for n in ("config", "data", "state", "cache", "runtime")))
            )
            try:
                service.tools.get("audio.get_volume").executor = lambda a, c: {"muted": False}
                task = asyncio.create_task(
                    service.request_tool(
                        {
                            "name": "agent.wait_for",
                            "arguments": {"conditions": CONDITIONS, "timeout_seconds": 1800},
                        },
                        "direct-wait",
                    )
                )
                for _ in range(100):
                    if any(e["type"] == "task.wait_state" for e in service.bus.history()):
                        break
                    await asyncio.sleep(0.01)
                self.assertIsNone(service.planner.active)
                service._action_generation += 1
                result = await asyncio.wait_for(task, 1)
                self.assertEqual(result["result"]["wait_state"], "CANCELLED", result)
            finally:
                service.memory.close()
