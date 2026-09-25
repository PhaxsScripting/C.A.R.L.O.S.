from __future__ import annotations

import logging
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "core"))

from ev.events import PhaxEventBus  # noqa: E402
from ev.tools import ToolContext, ValidationError  # noqa: E402
from ev.tools.builtin import (  # noqa: E402
    copy_path,
    create_directory,
    create_text_file,
    hash_file,
    move_path,
    resolve_destination,
    trash_path,
)


class FileActionTests(unittest.TestCase):
    def context(self, root: Path) -> ToolContext:
        return ToolContext(
            {"security": {"allowed_roots": [str(root)], "max_tool_output_bytes": 262144}},
            PhaxEventBus(),
            logging.getLogger("test"),
        )

    def test_create_copy_move_and_hash_verify_exact_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = self.context(root)
            directory = root / "organized"
            created_directory = create_directory(
                {"path": str(directory), "parents": False}, context
            )
            self.assertTrue(created_directory["verified"])
            source = directory / "note.txt"
            created_file = create_text_file(
                {"path": str(source), "content": "hello E.V.\n"}, context
            )
            self.assertTrue(created_file["verified"])
            source_hash = hash_file({"path": str(source)}, context)["sha256"]
            copied = root / "note-copy.txt"
            copied_result = copy_path({"source": str(source), "destination": str(copied)}, context)
            self.assertTrue(copied_result["verified"])
            self.assertEqual(copied_result["sha256"], source_hash)
            moved = root / "renamed.txt"
            moved_result = move_path({"source": str(copied), "destination": str(moved)}, context)
            self.assertTrue(moved_result["verified"])
            self.assertFalse(copied.exists())

    def test_no_file_action_overwrites_an_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = self.context(root)
            first = root / "first.txt"
            second = root / "second.txt"
            first.write_text("first", encoding="utf-8")
            second.write_text("second", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "will not overwrite"):
                copy_path({"source": str(first), "destination": str(second)}, context)
            with self.assertRaisesRegex(ValidationError, "will not overwrite"):
                move_path({"source": str(first), "destination": str(second)}, context)
            with self.assertRaisesRegex(ValidationError, "will not overwrite"):
                create_text_file({"path": str(second), "content": "replacement"}, context)
            self.assertEqual(second.read_text(encoding="utf-8"), "second")

    def test_destination_cannot_escape_through_existing_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as outside:
            root = Path(temporary)
            (root / "escape").symlink_to(Path(outside), target_is_directory=True)
            with self.assertRaisesRegex(ValidationError, "outside E.V. allowed roots"):
                resolve_destination(str(root / "escape" / "file.txt"), self.context(root))

    def test_allowed_root_itself_can_never_be_trashed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValidationError, "allowed-root"):
                trash_path({"path": str(root)}, self.context(root))


if __name__ == "__main__":
    unittest.main()
