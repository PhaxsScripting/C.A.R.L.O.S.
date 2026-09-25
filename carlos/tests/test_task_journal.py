import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from ev.ai.base import ProviderTurn, ToolCall
from ev.paths import Paths
from ev.service import CarlosCore
from ev.task_journal import TaskJournal, private_summary
from ev.ipc.server import _is_responsive_request


class TaskJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "tasks.db"
        self.journal = TaskJournal(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_private_database_and_receipts_survive_reopen(self):
        self.journal.begin("task", "open my project")
        step = self.journal.start_step("task", "applications.open", {"desktop_id": "code"})
        self.journal.finish_step(
            step,
            {"status": "completed", "result": {"launched": True}, "execution": {"verified": False}},
        )
        self.journal.finish(
            "task", {"status": "completed", "execution_status": "EXECUTED_UNVERIFIED"}
        )
        reopened = TaskJournal(self.path)
        result = reopened.get("task")
        self.assertEqual(result["status"], "EXECUTED_UNVERIFIED")
        self.assertEqual(result["steps"][0]["receipt"]["evidence"], {"launched": True})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_file_recovery_receipts_keep_backup_and_hashes_not_edited_text(self):
        self.journal.begin("edit", "edit the note")
        step = self.journal.start_step(
            "edit",
            "files.text_replace",
            {"old_text": "private old content", "new_text": "private new content"},
        )
        self.journal.finish_step(
            step,
            {
                "status": "completed",
                "result": {
                    "verified": True,
                    "backup": "/notes/.ev-text-backup-test/original",
                    "sha256": "a" * 64,
                    "content": "private file content",
                },
            },
        )
        task = self.journal.get("edit")
        receipt = task["steps"][0]["receipt"]["evidence"]
        self.assertEqual(receipt["backup"], "/notes/.ev-text-backup-test/original")
        self.assertEqual(receipt["sha256"], "a" * 64)
        for value in ("private old content", "private new content", "private file content"):
            self.assertNotIn(value, json.dumps(task))

    def test_restart_marks_inflight_effect_uncertain_without_replaying(self):
        self.journal.begin("task", "move a file")
        self.journal.start_step("task", "files.move", {"path": "/test"})
        self.assertEqual(self.journal.recover_interrupted(), 1)
        task = self.journal.get("task")
        self.assertEqual(task["status"], "INTERRUPTED")
        self.assertEqual(task["steps"][0]["status"], "INTERRUPTED_UNCERTAIN")
        self.assertEqual(self.journal.recover_interrupted(), 0)

    def test_cancelled_inflight_action_has_uncertain_effect(self):
        self.journal.begin("task", "move a file")
        self.journal.start_step("task", "files.move", {})
        self.journal.finish("task", {"status": "cancelled"})
        self.assertEqual(self.journal.get("task")["steps"][0]["status"], "INTERRUPTED_UNCERTAIN")
        self.assertIsNone(self.journal.latest_resumable())

    def test_completed_or_cancelled_task_is_not_implicitly_resumed(self):
        for status in ("completed", "cancelled", "denied", "expired", "confirmation_required"):
            self.journal.begin(status, "task")
            self.journal.finish(status, {"status": status})
            self.assertIsNone(self.journal.latest_resumable())

    def test_new_conversation_does_not_resurrect_an_old_failure(self):
        self.journal.begin("old", "delete some files")
        self.journal.finish("old", {"status": "failed"})
        self.journal.begin("new", "tell me about Python")
        self.journal.finish("new", {"status": "completed"})
        self.assertIsNone(self.journal.latest_resumable())

    def test_redacted_or_truncated_goal_cannot_resume_without_restatement(self):
        for index, request in enumerate(("a" * 3000, "use api_key=secretvalue")):
            self.journal.begin(str(index), request)
            self.journal.finish(str(index), {"status": "failed"})
            self.assertIsNone(self.journal.latest_resumable())

    def test_resume_is_historical_context_not_old_approval(self):
        self.journal.begin("old", "open project")
        self.journal.finish("old", {"status": "failed"})
        task = self.journal.latest_resumable()
        context = self.journal.continuation_context(task)
        self.assertIn("Do not replay", context)
        self.assertIn("historical data", context)
        self.assertIn("open project", context)

    def test_sensitive_values_and_tool_output_are_not_saved(self):
        value = {
            "text": "typed password",
            "api_key": "secretvalue",
            "authorization": "Bearer abc",
            "approval_token": "approval-secret",
            "stdout": "sensitive source code",
            "url": "https://example.com/?token=abcd",
            "note": "nvapi-test-key123456789",
        }
        encoded = json.dumps(private_summary(value))
        for secret in (
            "typed password",
            "secretvalue",
            "Bearer abc",
            "approval-secret",
            "sensitive source code",
            "token=abcd",
            "nvapi-test-key123456789",
        ):
            self.assertNotIn(secret, encoded)

    def test_direct_diagnostic_tool_does_not_create_implicit_task(self):
        self.assertIsNone(self.journal.start_step("unknown", "system.identity", {}))
        self.assertEqual(self.journal.recent(), [])

    def test_duplicate_task_id_cannot_replay_an_existing_attempt(self):
        self.journal.begin("one", "test")
        with self.assertRaises(sqlite3.IntegrityError):
            self.journal.begin("one", "replay")

    def test_task_inspection_remains_responsive_during_execution(self):
        for kind in ("agent.tasks.list", "agent.tasks.get"):
            self.assertTrue(_is_responsive_request({"type": kind, "payload": {}}))

    def test_restart_does_not_restore_pending_authorization(self):
        self.journal.begin("one", "coding change")
        self.journal.finish(
            "one",
            {"status": "confirmation_required", "confirmation": {"approval_token": "never-save"}},
        )
        self.journal.recover_interrupted()
        record = self.journal.get("one")
        self.assertEqual(record["status"], "INTERRUPTED")
        self.assertNotIn("never-save", json.dumps(record))

    def test_paginated_receipts_are_complete_ordered_and_do_not_repeat_summary(self):
        self.journal.begin("task", "fixture")
        identifiers = []
        for i in range(13):
            step = self.journal.start_step("task", "files.info", {"path": f"/fixture/{i}"})
            self.journal.finish_step(
                step, {"status": "completed", "result": {"path": f"/fixture/{i}"}}
            )
            identifiers.append(step)
        self.journal.finish(
            "task", {"status": "failed", "tool_receipts": [{"tool": "files.info"}] * 40}
        )
        read, offset = [], 0
        while offset is not None:
            page = self.journal.get_page("task", offset, 5)
            self.assertEqual(page["steps_total"], 13)
            self.assertLessEqual(len(page["steps"]), 5)
            self.assertTrue(page["history_partial"])
            self.assertTrue(page["detail_receipts_omitted"])
            self.assertNotIn("tool_receipts", page["detail"])
            read.extend(step["id"] for step in page["steps"])
            offset = page["next_step_offset"]
        self.assertEqual(read, identifiers)
        self.assertIn("tool_receipts", self.journal.get("task")["detail"])
        self.assertEqual(len(self.journal.get("task")["steps"]), 13)

    def test_page_limits_and_unknown_ids_fail_safely(self):
        self.assertIsNone(self.journal.get_page("missing"))
        for offset, limit in ((-1, 5), (257, 5), (True, 5), (0, 0), (0, 21), (0, True)):
            with self.assertRaises(ValueError):
                self.journal.get_page("missing", offset, limit)

    def test_pages_preserve_uncertainty_redaction_and_empty_tail(self):
        self.journal.begin("task", "fixture")
        self.journal.start_step("task", "files.move", {"text": "secret", "token": "secret"})
        self.journal.recover_interrupted()
        page = self.journal.get_page("task", 0, 1)
        self.assertFalse(page["history_partial"])
        self.assertEqual(page["steps"][0]["status"], "INTERRUPTED_UNCERTAIN")
        self.assertNotIn("secret", json.dumps(page))
        tail = self.journal.get_page("task", 256, 1)
        self.assertEqual(tail["steps"], [])
        self.assertEqual(tail["steps_total"], 1)
        self.assertIsNone(tail["next_step_offset"])
        self.assertTrue(tail["history_partial"])

    def test_page_boundary_excludes_steps_added_by_ongoing_self_inspection(self):
        self.journal.begin("task", "fixture")
        for _ in range(3):
            self.journal.start_step("task", "files.info", {})
        first = self.journal.get_page("task", limit=2)
        self.journal.start_step("task", "agent.task_status", {})
        second = self.journal.get_page(
            "task", offset=2, limit=2, through_step_id=first["steps_through_id"]
        )
        self.assertEqual(second["steps_total"], 3)
        self.assertEqual(len(second["steps"]), 1)
        self.assertIsNone(second["next_step_offset"])
        self.assertEqual(self.journal.get_page("task")["steps_total"], 4)
        for boundary in (-1, True, 2**63):
            with self.assertRaises(ValueError):
                self.journal.get_page("task", through_step_id=boundary)


class TaskJournalIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.service = CarlosCore(
            paths=Paths(*(root / n for n in ("config", "data", "state", "cache", "runtime")))
        )
        self.service.tools.execute = AsyncMock(side_effect=AssertionError("No live mutations"))

    async def asyncTearDown(self):
        self.service.memory.close()
        self.temp.cleanup()

    async def test_real_service_records_plan_before_and_after_execution(self):
        async def execute(spec, arguments):
            stored = self.service.task_journal.get("record")
            self.assertEqual(stored["steps"][-1]["status"], "RUNNING")
            return {"verified": True}

        self.service.tools.execute.side_effect = execute
        result = await self.service._submit_action_clauses("pause Spotify", "record")
        self.assertEqual(result["status"], "completed")
        stored = self.service.task_journal.get("record")
        self.assertEqual(stored["status"], "PLAN_COMPLETED")
        self.assertEqual(stored["steps"][0]["status"], "COMPLETED")

    async def test_continue_observes_before_model_and_links_parent(self):
        journal = self.service.task_journal
        journal.begin("old", "get my project ready")
        journal.finish("old", {"status": "failed"})
        self.service.tools.execute.side_effect = None
        self.service.tools.execute.return_value = {"active_window_id": "current", "windows": []}
        self.service.brain.submit = AsyncMock(
            return_value={"status": "completed", "execution_status": "EXECUTED_UNVERIFIED"}
        )
        result = await self.service._submit_action_clauses("continue", "new")
        self.assertEqual(result["task_id"], "new")
        self.assertEqual(self.service.tools.execute.await_args.args[0].name, "desktop.observe")
        self.service.tools.execute.assert_awaited_once()
        call = self.service.brain.submit.await_args
        self.assertEqual(call.args, ("continue", "new"))
        self.assertIn("get my project ready", call.kwargs["resume_context"])
        self.assertEqual(journal.get("new")["parent_id"], "old")

    async def test_continue_stops_when_observation_fails(self):
        self.service.task_journal.begin("old", "get my project ready")
        self.service.task_journal.finish("old", {"status": "failed"})
        self.service.tools.execute.side_effect = RuntimeError("No desktop")
        self.service.brain.submit = AsyncMock()
        result = await self.service._submit_action_clauses("keep going", "new")
        self.assertEqual(result["status"], "failed")
        self.service.brain.submit.assert_not_awaited()

    async def test_continue_with_no_task_runs_nothing(self):
        self.service.brain.submit = AsyncMock()
        result = await self.service._submit_action_clauses("continue", "empty")
        self.assertEqual(result["status"], "failed")
        self.service.tools.execute.assert_not_awaited()
        self.service.brain.submit.assert_not_awaited()

    async def test_cancelled_request_is_saved_not_left_running(self):
        async def cancelled(*args):
            raise asyncio.CancelledError()

        self.service._submit_action_clauses_impl = cancelled
        with self.assertRaises(asyncio.CancelledError):
            await self.service._submit_action_clauses("task", "cancelled")
        self.assertEqual(self.service.task_journal.get("cancelled")["status"], "CANCELLED")

    async def test_read_only_ipc_exposes_receipts(self):
        self.service.task_journal.begin("task", "test")
        result = await self.service.handle_request(
            {"type": "agent.tasks.get", "payload": {"id": "task"}, "id": "inspect"}
        )
        self.assertEqual(result["task"]["request"], "test")
        self.service.tools.execute.assert_not_awaited()

    async def test_model_history_tools_are_read_only_and_bound_to_journal(self):
        self.service.task_journal.begin("task", "test")
        spec, args = self.service.tools.validate("agent.task_status", {"id": "task"})
        self.assertTrue(spec.read_only)
        result = spec.executor(args, self.service.tools.context)
        self.assertEqual(result["task"]["id"], "task")
        self.assertFalse(result["saved_approvals_reusable"])
        self.assertFalse(spec.executor({"id": "missing"}, self.service.tools.context)["ok"])

    async def test_stop_during_journal_creation_prevents_execution(self):
        begin = self.service.task_journal.begin

        def stop_at_begin(*args):
            begin(*args)
            self.service._action_generation += 1

        self.service.task_journal.begin = stop_at_begin
        self.service._submit_action_clauses_impl = AsyncMock()
        result = await self.service._submit_action_clauses("open Firefox", "stopped")
        self.assertEqual(result["status"], "cancelled")
        self.service._submit_action_clauses_impl.assert_not_awaited()
        self.assertEqual(self.service.task_journal.get("stopped")["status"], "CANCELLED")

    async def test_brain_receives_resume_context_without_changing_user_text(self):
        self.service.brain.provider.begin = AsyncMock(
            return_value=ProviderTurn("test", "test", "Ready.")
        )
        await self.service.brain.submit("continue", "resume", resume_context="Prior task evidence")
        arguments = self.service.brain.provider.begin.await_args.args
        self.assertEqual(arguments[0], "continue")
        self.assertEqual(arguments[1][-1], {"role": "user", "content": "Prior task evidence"})
