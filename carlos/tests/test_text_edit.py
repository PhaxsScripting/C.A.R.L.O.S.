import asyncio
import hashlib
import logging
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from ev.ai.base import ProviderTurn, ToolCall
from ev.ai.nvidia import NvidiaProvider
from ev.events import PhaxEventBus
from ev.paths import Paths
from ev.service import CarlosCore
from ev.tools import ToolContext
from ev.tools.base import ValidationError
from ev.tools.text_edit import _replace, _snapshot, replace_text


class TextEditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "note.txt"
        self.original = b"hello world\r\nsecond line\n"
        self.path.write_bytes(self.original)
        self.context = ToolContext(
            {"security": {"allowed_roots": [str(self.root)], "max_file_read_bytes": 65536}},
            PhaxEventBus(),
            logging.getLogger("text-edit-test"),
        )
        self.arguments = {
            "path": str(self.path),
            "expected_sha256": hashlib.sha256(self.original).hexdigest(),
            "old_text": "hello",
            "new_text": "goodbye",
        }

    def execute(self):
        return _replace(self.arguments, self.context, threading.Event())

    def test_exact_replace_preserves_line_endings_mode_and_backup(self):
        self.path.chmod(0o640)
        result = self.execute()
        self.assertTrue(result["verified"])
        self.assertEqual(self.path.read_bytes(), b"goodbye world\r\nsecond line\n")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)
        backup = Path(result["backup"])
        self.assertEqual(backup.read_bytes(), self.original)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(backup.parent.stat().st_mode), 0o700)
        self.assertEqual(list(self.root.glob(".ev-text-stage-*")), [])

    def test_stale_revision_preserves_original_without_backup(self):
        self.arguments["expected_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValidationError, "revision"):
            self.execute()
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(list(self.root.glob(".ev-text-backup-*")), [])

    def test_missing_duplicate_and_empty_matches_rejected(self):
        for old in ("missing", "l", ""):
            with self.subTest(old=old):
                self.arguments["old_text"] = old
                with self.assertRaisesRegex(ValidationError, "exactly once"):
                    self.execute()
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_symlink_and_hardlink_rejected(self):
        link = self.root / "link"
        link.symlink_to(self.path)
        self.arguments["path"] = str(link)
        with self.assertRaises(ValidationError):
            self.execute()
        self.arguments["path"] = str(self.path)
        os.link(self.path, self.root / "hardlink")
        with self.assertRaises(ValidationError):
            self.execute()

    def test_utf8_and_binary_rejected(self):
        for data in (b"hello\0", b"hello\xff"):
            with self.subTest(data=data):
                self.path.write_bytes(data)
                self.arguments["expected_sha256"] = hashlib.sha256(data).hexdigest()
                with self.assertRaises(ValidationError):
                    self.execute()
                self.assertEqual(self.path.read_bytes(), data)

    def test_new_nul_oversized_and_unchanged_content_rejected(self):
        for new in ("hello", "\0", "a" * 65536):
            with self.subTest(new_length=len(new)):
                self.arguments["new_text"] = new
                with self.assertRaises(ValidationError):
                    self.execute()
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_concurrent_edit_detected_and_preserved(self):
        calls = 0

        def changed(path, maximum):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.path.write_text("external edit")
            return _snapshot(path, maximum)

        with patch("ev.tools.text_edit._snapshot", side_effect=changed), self.assertRaisesRegex(
            ValidationError, "changed"
        ):
            self.execute()
        self.assertEqual(self.path.read_text(), "external edit")
        self.assertEqual(
            next(self.root.glob(".ev-text-backup-*/original")).read_bytes(), self.original
        )

    def test_final_readback_mismatch_is_not_success(self):
        calls = 0

        def changed(path, maximum):
            nonlocal calls
            calls += 1
            if calls == 3:
                self.path.write_text("external edit after publication")
            return _snapshot(path, maximum)

        with patch("ev.tools.text_edit._snapshot", side_effect=changed):
            result = self.execute()
        self.assertFalse(result["verified"])
        self.assertIn("error", result)

    def test_failed_replace_retains_original_and_reports_backup(self):
        with patch(
            "ev.tools.text_edit.os.replace", side_effect=OSError("denied")
        ), self.assertRaisesRegex(ValidationError, "Recovery backup"):
            self.execute()
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(list(self.root.glob(".ev-text-stage-*")), [])

    def test_cancelled_edit_does_not_write(self):
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaisesRegex(ValidationError, "cancelled"):
            _replace(self.arguments, self.context, cancelled)
        self.assertEqual(self.path.read_bytes(), self.original)


class FileToolsIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = CarlosCore(
            paths=Paths(*(self.root / n for n in ("config", "data", "state", "cache", "runtime")))
        )
        self.service.config["security"]["allowed_roots"] = [str(self.root)]
        self.provider = NvidiaProvider({"model": "test"})
        self.provider._request = AsyncMock(side_effect=AssertionError("No real network calls"))
        self.service.brain.provider = self.provider

    async def asyncTearDown(self):
        self.service.memory.close()
        self.temp.cleanup()

    async def test_model_edit_requires_confirmation_then_executes_and_returns_real_hash(self):
        path = self.root / "note.txt"
        path.write_text("hello")
        args = {
            "path": str(path),
            "expected_sha256": hashlib.sha256(b"hello").hexdigest(),
            "old_text": "hello",
            "new_text": "goodbye",
        }
        self.provider.begin = AsyncMock(
            return_value=ProviderTurn(
                "nvidia", "test", "", [ToolCall("edit", "files.text_replace", args)]
            )
        )
        self.provider.continue_with_tools = AsyncMock(
            return_value=ProviderTurn("nvidia", "test", "Done.")
        )
        result = await self.service.brain.submit("Replace hello with goodbye in that note")
        self.assertEqual(result["status"], "confirmation_required")
        self.assertEqual(path.read_text(), "hello")
        pending = result["confirmation"]
        confirmed = await self.service.resolve_confirmation(
            {"id": pending["id"], "approval_token": pending["approval_token"], "approved": True}
        )
        self.assertEqual(confirmed["command"]["status"], "completed")
        self.assertEqual(path.read_text(), "goodbye")
        call, receipt = self.provider.continue_with_tools.await_args.args[1][0]
        self.assertEqual(call.name, "files.text_replace")
        self.assertTrue(receipt["verification"]["verified"])
        self.assertEqual(receipt["result"]["sha256"], hashlib.sha256(b"goodbye").hexdigest())

    async def test_model_archive_uses_real_planner_and_verified_temp_extraction(self):
        import zipfile

        source = self.root / "input.zip"
        destination = self.root / "unpacked"
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("note.txt", "verified fixture")
        result = await self.service._request_model_tool(
            {
                "name": "files.archive_extract",
                "arguments": {"path": str(source), "destination": str(destination)},
            },
            "archive-fixture",
        )
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["verification"]["verified"])
        self.assertEqual((destination / "note.txt").read_text(), "verified fixture")
        self.assertEqual(self.service.planner.recent[-1].steps[0].tool, "files.archive_extract")

    async def test_cancelled_async_edit_signals_worker(self):
        entered, stopped = threading.Event(), threading.Event()

        def worker(arguments, context, cancelled):
            entered.set()
            if cancelled.wait(2):
                stopped.set()

        with patch("ev.tools.text_edit._replace", side_effect=worker):
            task = asyncio.create_task(replace_text({}, None))
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(await asyncio.to_thread(stopped.wait, 2))
