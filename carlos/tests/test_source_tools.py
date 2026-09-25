import hashlib
import logging
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ev.events import PhaxEventBus
from ev.tools import ToolContext
from ev.tools.base import ValidationError
from ev.tools.results import evaluate_result
from ev.tools.source import inspect_source, open_source


class SourceToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "main file.cpp"
        self.path.write_text("int main() {\n  wrong name;\n}\n")
        self.context = ToolContext(
            {"security": {"allowed_roots": [str(self.root)], "max_file_read_bytes": 65536}},
            PhaxEventBus(),
            logging.getLogger("source"),
        )
        self.args = {"path": str(self.path), "line": 2}

    def test_numbered_source_context_and_hash_match_actual_file(self):
        result = inspect_source({**self.args, "context_lines": 0}, self.context)
        self.assertEqual(result["lines"], [{"line": 2, "text": "  wrong name;"}])
        self.assertEqual(result["sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertTrue(result["content_is_untrusted"])

    def test_missing_line_binary_link_and_outside_root_are_rejected(self):
        with self.assertRaises(ValidationError):
            inspect_source({**self.args, "line": 40}, self.context)
        link = self.root / "link"
        link.symlink_to(self.path)
        with self.assertRaises(ValidationError):
            inspect_source({**self.args, "path": str(link)}, self.context)
        with self.assertRaises(ValidationError):
            inspect_source({"path": "/etc/passwd", "line": 1}, self.context)
        self.path.write_bytes(b"binary\0")
        with self.assertRaises(ValidationError):
            inspect_source({**self.args, "line": 1}, self.context)

    def test_editor_handoff_fixed_argv_not_claimed_caret_verification(self):
        args = {
            **self.args,
            "expected_sha256": inspect_source(self.args, self.context)["sha256"],
            "column": 3,
        }
        with patch(
            "ev.tools.source.subprocess.run", return_value=SimpleNamespace(returncode=0)
        ) as run, patch("ev.tools.source.os.access", return_value=True), patch(
            "ev.tools.source.Path.is_file", return_value=True
        ):
            result = open_source(args, self.context)
        self.assertEqual(
            run.call_args.args[0], ["/usr/bin/code", "--reuse-window", "--goto", f"{self.path}:2:3"]
        )
        self.assertTrue(result["requested"])
        self.assertFalse(evaluate_result("development.source.open", result).verified)

    def test_changed_source_does_not_launch_editor(self):
        args = {**self.args, "expected_sha256": inspect_source(self.args, self.context)["sha256"]}
        self.path.write_text("new version\nnew code\n")
        with patch("ev.tools.source.subprocess.run") as run, self.assertRaisesRegex(
            ValidationError, "changed"
        ):
            open_source(args, self.context)
        run.assert_not_called()

    def test_handoff_timeout_is_uncertain_and_not_retried(self):
        args = {**self.args, "expected_sha256": inspect_source(self.args, self.context)["sha256"]}
        with patch(
            "ev.tools.source.subprocess.run", side_effect=subprocess.TimeoutExpired("code", 3)
        ) as run, patch("ev.tools.source.os.access", return_value=True), patch(
            "ev.tools.source.Path.is_file", return_value=True
        ):
            result = open_source(args, self.context)
        self.assertTrue(result["uncertain"])
        run.assert_called_once()
