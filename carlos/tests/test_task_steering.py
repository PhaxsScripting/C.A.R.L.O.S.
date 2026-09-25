import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from ev.cli import parser
from ev.ipc.server import _is_responsive_request
from ev.paths import Paths
from ev.service import CarlosCore


class SteeringTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.service = CarlosCore(
            paths=Paths(*(root / n for n in ("config", "data", "state", "cache", "runtime")))
        )
        self.service._prepare_interactive_request = AsyncMock()
        self.service.tools.execute = AsyncMock(
            return_value={"active_window_id": "fresh", "windows": []}
        )
        self.service.brain.submit = AsyncMock(
            return_value={"status": "completed", "execution_status": "EXECUTED_UNVERIFIED"}
        )

    async def asyncTearDown(self):
        self.service.memory.close()
        self.temp.cleanup()

    async def test_revision_links_exact_parent_and_observes_before_model(self):
        journal = self.service.task_journal
        journal.begin("old", "prepare project")
        journal.finish("old", {"status": "failed"})
        result = await self.service._steer_task("old", "Use the other project instead", "revised")
        self.assertEqual(result["task_id"], "revised")
        self.assertEqual(journal.get("revised")["parent_id"], "old")
        self.assertEqual(journal.get("revised")["request"], "Use the other project instead")
        self.assertEqual(self.service.tools.execute.await_args.args[0].name, "desktop.observe")
        self.assertIn(
            "prepare project", self.service.brain.submit.await_args.kwargs["resume_context"]
        )
        self.assertIn(
            "Do not replay", self.service.brain.submit.await_args.kwargs["resume_context"]
        )

    async def test_revision_releases_steering_lock_before_execution(self):
        self.service.task_journal.begin("old", "prepare project")
        self.service.task_journal.finish("old", {"status": "failed"})

        async def revised(*args, **kwargs):
            self.assertFalse(self.service._steering_lock.locked())
            return {"status": "completed"}

        self.service.brain.submit.side_effect = revised
        await self.service._steer_task("old", "use other folder", "new")

    async def test_unrelated_running_task_is_not_interrupted(self):
        self.service.task_journal.begin("old", "first task")
        self.service._interactive_task = asyncio.create_task(asyncio.sleep(10))
        self.service._interactive_correlation = "other"
        try:
            result = await self.service._steer_task("old", "change it", "new")
            self.assertEqual(result["status"], "busy")
            self.assertFalse(self.service._interactive_task.cancelled())
            self.service.brain.submit.assert_not_awaited()
        finally:
            self.service._interactive_task.cancel()
            await asyncio.gather(self.service._interactive_task, return_exceptions=True)

    async def test_pending_approval_and_redacted_history_cannot_be_reused(self):
        journal = self.service.task_journal
        for task_id, text, status in (
            ("secret", "api_key=hiddenvalue", "failed"),
            ("permission", "run coding task", "confirmation_required"),
        ):
            journal.begin(task_id, text)
            journal.finish(task_id, {"status": status})
            result = await self.service._steer_task(task_id, "do it now", "new")
            self.assertEqual(result["status"], "blocked")
        self.service.brain.submit.assert_not_awaited()

    async def test_missing_task_and_invalid_text_never_execute(self):
        self.assertEqual(
            (await self.service._steer_task("missing", "change it", "new"))["status"], "failed"
        )
        for text in ("", None, "x" * 8001):
            with self.assertRaises(ValueError):
                await self.service._steer_task("missing", text, "new")
        self.service.tools.execute.assert_not_awaited()

    async def test_live_task_revision_cancels_wait_preserves_receipts_and_reobserves(self):
        # Execute the real wait tool and planner, but no desktop mutations.
        execute = self.service.tools.execute

        async def selective(spec, arguments):
            if spec.name == "agent.wait_for":
                return await spec.executor(arguments, self.service.tools.context)
            if spec.name == "audio.get_volume":
                return {"muted": False}
            return {"active_window_id": "fresh", "windows": []}

        execute.side_effect = selective

        async def start_wait(text, correlation):
            return await self.service._request_model_tool(
                {
                    "name": "agent.wait_for",
                    "arguments": {
                        "conditions": [{"kind": "audio_muted", "expected": True}],
                        "timeout_seconds": 1800,
                    },
                },
                correlation,
            )

        self.service._submit_action_clauses_impl = start_wait
        original = asyncio.create_task(
            self.service._submit_action_clauses("wait for muted audio", "old")
        )
        try:
            for _ in range(200):
                if any(c.args[0].name == "audio.get_volume" for c in execute.await_args_list):
                    break
                await asyncio.sleep(0.01)
            result = await asyncio.wait_for(
                self.service._steer_task(
                    "old", "Actually inspect the current desktop instead", "new"
                ),
                2,
            )
            self.assertEqual(result["status"], "completed", result)
            self.assertEqual((await original)["status"], "cancelled")
            journal = self.service.task_journal
            self.assertEqual(journal.get("old")["status"], "CANCELLED")
            self.assertEqual(journal.get("new")["parent_id"], "old")
            self.assertTrue(journal.get("old")["steps"])
            self.assertIn(
                "supersedes incompatible older instructions",
                self.service.brain.submit.await_args.kwargs["resume_context"],
            )
        finally:
            if not original.done():
                original.cancel()
            await asyncio.gather(original, return_exceptions=True)

    async def test_failed_fresh_observation_blocks_revision(self):
        self.service.task_journal.begin("old", "first task")
        self.service.task_journal.finish("old", {"status": "failed"})
        self.service.tools.execute.return_value = {"ok": False, "error": "No desktop connection"}
        result = await self.service._steer_task("old", "revise", "new")
        self.assertNotEqual(result["status"], "completed")
        self.service.brain.submit.assert_not_awaited()

    def test_steering_is_responsive_and_explicit_cli_not_a_model_tool(self):
        self.assertTrue(_is_responsive_request({"type": "agent.tasks.steer", "payload": {}}))
        args = parser().parse_args(["steer", "exact-task-id", "use the other folder"])
        self.assertEqual(args.id, "exact-task-id")
        self.assertNotIn("agent.tasks.steer", [t["name"] for t in self.service.tools.catalog()])
