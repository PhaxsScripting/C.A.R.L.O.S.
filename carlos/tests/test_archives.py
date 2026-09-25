import asyncio
import gzip
import io
import logging
import os
import stat
import tarfile
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from ev.events import PhaxEventBus
from ev.tools import ToolContext, ToolRegistry
from ev.tools.archives import _operation, _publish, extract_archive, register_archive_tools
from ev.tools.base import ValidationError
from ev.tools.builtin import copy_path, read_file
from ev.tools.results import evaluate_result


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.context = ToolContext(
            {
                "security": {
                    "allowed_roots": [str(self.root)],
                    "max_file_read_bytes": 64,
                    "max_tool_output_bytes": 65536,
                }
            },
            PhaxEventBus(),
            logging.getLogger("archives-test"),
        )
        self.path, self.destination = self.root / "input.zip", self.root / "output"

    def make_zip(self, entries=None):
        with zipfile.ZipFile(self.path, "w") as archive:
            for name, value in entries or [("folder/hello.txt", b"hello\n"), ("empty/", b"")]:
                archive.writestr(name, value)

    def run_tool(self, *, extract=True, cancelled=None):
        return _operation(
            {"path": str(self.path), "destination": str(self.destination)},
            self.context,
            cancelled or threading.Event(),
            extract=extract,
        )

    def test_zip_extract_verified_contents_and_private_permissions(self):
        self.make_zip()
        result = self.run_tool()
        self.assertEqual((self.destination / "folder/hello.txt").read_bytes(), b"hello\n")
        self.assertTrue((self.destination / "empty").is_dir())
        self.assertTrue(result["verified"])
        self.assertEqual(result["files_verified"], 1)
        self.assertEqual(
            stat.S_IMODE((self.destination / "folder/hello.txt").stat().st_mode), 0o600
        )
        self.assertEqual(stat.S_IMODE(self.destination.stat().st_mode), 0o700)
        self.assertTrue(evaluate_result("files.archive_extract", result).verified)
        self.assertEqual(list(self.root.glob(".ev-extract-*")), [])

    def test_inspection_is_metadata_only_and_creates_no_destination(self):
        self.make_zip()
        result = self.run_tool(extract=False)
        self.assertFalse(result["payload_verified"])
        self.assertEqual(result["entries_count"], 2)
        self.assertFalse(self.destination.exists())

    def test_tar_and_gzip_roundtrip(self):
        for mode in ("w", "w:gz"):
            with self.subTest(mode=mode):
                self.destination = self.root / mode.replace(":", "-")
                with tarfile.open(self.path, mode) as archive:
                    member = tarfile.TarInfo("nested/a.txt")
                    member.size = 5
                    member.mode = 0o777
                    archive.addfile(member, io.BytesIO(b"hello"))
                self.assertTrue(self.run_tool()["verified"])
                self.assertEqual((self.destination / "nested/a.txt").read_bytes(), b"hello")

    def test_common_tar_dot_prefix_and_root_marker(self):
        with tarfile.open(self.path, "w") as archive:
            root = tarfile.TarInfo(".")
            root.type = tarfile.DIRTYPE
            archive.addfile(root)
            member = tarfile.TarInfo("./nested/a.txt")
            member.size = 5
            archive.addfile(member, io.BytesIO(b"hello"))
        self.assertTrue(self.run_tool()["verified"])
        self.assertEqual((self.destination / "nested/a.txt").read_bytes(), b"hello")

    def test_compressed_zip_expansion_ratio_rejected(self):
        with zipfile.ZipFile(self.path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("bomb", b"a" * (2 * 1024 * 1024))
        with self.assertRaisesRegex(ValidationError, "ratio"):
            self.run_tool()
        self.assertFalse(self.destination.exists())

    def test_cancellation_during_staging_cleans_up_without_publication(self):
        self.make_zip()
        cancelled = threading.Event()
        import ev.tools.archives as module

        actual_check = module._check

        def cancel_after_file_written(deadline, event):
            if list(self.root.glob(".ev-extract-*/payload/folder/hello.txt")):
                cancelled.set()
            actual_check(deadline, event)

        with patch(
            "ev.tools.archives._check", side_effect=cancel_after_file_written
        ), self.assertRaisesRegex(ValidationError, "cancelled"):
            self.run_tool(cancelled=cancelled)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.root.glob(".ev-extract-*")), [])

    def test_path_traversal_absolute_and_ambiguous_names_rejected(self):
        for name in (
            "../escape",
            "/absolute",
            "a/../../escape",
            "C:/drive",
            "a\\..\\escape",
            "a//b",
            "a/./b",
        ):
            with self.subTest(name=name):
                self.make_zip([(name, b"unsafe")])
                with self.assertRaises(ValidationError):
                    self.run_tool()
                self.assertFalse(self.destination.exists())

    def test_zip_symlink_rejected(self):
        with zipfile.ZipFile(self.path, "w") as archive:
            link = zipfile.ZipInfo("link")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, "../outside")
        with self.assertRaisesRegex(ValidationError, "linked"):
            self.run_tool()

    def test_tar_links_devices_fifo_and_sparse_rejected(self):
        for kind in (
            tarfile.SYMTYPE,
            tarfile.LNKTYPE,
            tarfile.CHRTYPE,
            tarfile.FIFOTYPE,
            tarfile.GNUTYPE_SPARSE,
        ):
            with self.subTest(kind=kind):
                with tarfile.open(self.path, "w") as archive:
                    member = tarfile.TarInfo("bad")
                    member.type = kind
                    member.linkname = "../outside"
                    archive.addfile(member)
                with self.assertRaises((ValidationError, tarfile.TarError)):
                    self.run_tool()
                self.assertFalse(self.destination.exists())

    def test_duplicate_and_file_parent_conflicts_rejected_before_writes(self):
        for entries in ([("a", b"one"), ("a", b"two")], [("a", b"one"), ("a/b", b"two")]):
            with self.subTest(entries=entries):
                self.make_zip(entries)
                with self.assertRaises(ValidationError):
                    self.run_tool()
                self.assertFalse(self.destination.exists())

    def test_existing_destination_not_overwritten(self):
        self.make_zip()
        self.destination.mkdir()
        marker = self.destination / "keep"
        marker.write_text("original")
        with self.assertRaises(ValidationError):
            self.run_tool()
        self.assertEqual(marker.read_text(), "original")

    def test_atomic_publish_refuses_racing_empty_directory(self):
        source = self.root / "stage"
        source.mkdir()
        self.destination.mkdir()
        with self.assertRaises(FileExistsError):
            _publish(source, self.destination)
        self.assertTrue(source.exists())

    def test_corrupt_crc_cleans_stage_and_publishes_nothing(self):
        self.make_zip([("a", b"uniquepayload")])
        self.path.write_bytes(self.path.read_bytes().replace(b"uniquepayload", b"changedvalue!"))
        with self.assertRaises(zipfile.BadZipFile):
            self.run_tool()
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.root.glob(".ev-extract-*")), [])

    def test_limits_apply_before_output(self):
        self.make_zip()
        for name, value in (
            ("MAX_ENTRIES", 1),
            ("MAX_INPUT", 1),
            ("MAX_EXPANDED", 1),
            ("MAX_SECONDS", 0),
        ):
            with self.subTest(limit=name), patch("ev.tools.archives." + name, value):
                with self.assertRaises(ValidationError):
                    self.run_tool()
                self.assertFalse(self.destination.exists())

    def test_gzip_expansion_bound_precedes_tar_parsing(self):
        self.path.write_bytes(gzip.compress(b"a" * 1000))
        with patch("ev.tools.archives.MAX_EXPANDED", 20), self.assertRaisesRegex(
            ValidationError, "expansion"
        ):
            self.run_tool()

    def test_pre_cancelled_operation_has_no_output(self):
        self.make_zip()
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaisesRegex(ValidationError, "cancelled"):
            self.run_tool(cancelled=cancelled)
        self.assertFalse(self.destination.exists())

    def test_publication_failure_cleans_staging(self):
        self.make_zip()
        with patch("ev.tools.archives._publish", side_effect=OSError("blocked")), self.assertRaises(
            OSError
        ):
            self.run_tool()
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.root.glob(".ev-extract-*")), [])

    def test_paths_outside_root_and_symlink_destination_rejected(self):
        self.make_zip()
        self.destination = self.root.parent / "ev-outside-root"
        with self.assertRaises(ValidationError):
            self.run_tool()
        self.destination = self.root / "link"
        self.destination.symlink_to(self.root / "missing")
        with self.assertRaises(ValidationError):
            self.run_tool()

    def test_tools_registered_with_honest_readonly_metadata(self):
        registry = ToolRegistry(self.context)
        register_archive_tools(registry)
        self.assertTrue(registry.get("files.archive_inspect").read_only)
        self.assertFalse(registry.get("files.archive_extract").read_only)

    def test_bounded_text_read_does_not_load_entire_file(self):
        self.path.write_bytes(b"a" * 1000)
        with patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded read")):
            result = read_file({"path": str(self.path)}, self.context)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["bytes"], 64)

    def test_directory_copy_rejects_nested_symlink_and_self_copy(self):
        source = self.root / "source"
        source.mkdir()
        (source / "link").symlink_to(self.path)
        with self.assertRaises(ValidationError):
            copy_path({"source": str(source), "destination": str(self.destination)}, self.context)
        self.assertFalse(self.destination.exists())
        with self.assertRaises(ValidationError):
            copy_path({"source": str(source), "destination": str(source / "copy")}, self.context)

    def test_directory_copy_verifies_contents_not_just_existence(self):
        source = self.root / "source"
        source.mkdir()
        (source / "a").write_text("original")
        import shutil

        actual_copy = shutil.copytree

        def corrupt(*args, **kwargs):
            result = actual_copy(*args, **kwargs)
            (self.destination / "a").write_text("corrupted")
            return result

        with patch("ev.tools.builtin.shutil.copytree", side_effect=corrupt):
            result = copy_path(
                {"source": str(source), "destination": str(self.destination)}, self.context
            )
        self.assertFalse(result["verified"])


class ArchiveCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_cancellation_signals_worker_to_stop_before_publish(self):
        entered, stopped = threading.Event(), threading.Event()

        def worker(arguments, context, cancelled, **kwargs):
            entered.set()
            if cancelled.wait(2):
                stopped.set()

        with patch("ev.tools.archives._operation", side_effect=worker):
            task = asyncio.create_task(extract_archive({}, None))
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(await asyncio.to_thread(stopped.wait, 2))
