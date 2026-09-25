from __future__ import annotations

import os
import signal
import stat
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "core"))

from ev.voice import stt  # noqa: E402


class WhisperServerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.runtime_dir = Path(self.temporary.name) / "runtime"
        self.runtime_dir.mkdir(mode=0o700)
        self.server_binary = Path(self.temporary.name) / "whisper-server"
        self.server_binary.touch(mode=0o700)
        self.model = Path(self.temporary.name) / "model.bin"
        self.model.touch(mode=0o600)
        self.adapter = stt.WhisperCppAdapter(
            {
                "server_binary": str(self.server_binary),
                "model": str(self.model),
                "server_port": 18082,
                "server_stop_timeout_seconds": 0.1,
            },
            self.runtime_dir,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def ownership(self) -> stt._ServerOwnership:
        uid = os.getuid()
        return stt._ServerOwnership(
            boot_id="test-boot",
            owner=stt._ProcessIdentity(
                pid=22001,
                uid=uid,
                exe="/usr/bin/python3",
                cmdline=("/usr/bin/python3", "-m", "ev"),
                start_time=101,
            ),
            server=stt._ProcessIdentity(
                pid=22002,
                uid=uid,
                exe=str(self.server_binary.resolve()),
                cmdline=tuple(self.adapter._server_command()),
                start_time=202,
            ),
        )

    def test_proc_snapshot_includes_uid_executable_command_line_and_start_time(self) -> None:
        identity = stt._process_identity(os.getpid())

        self.assertIsNotNone(identity)
        assert identity is not None
        self.assertEqual(identity.uid, os.getuid())
        self.assertEqual(identity.exe, os.path.realpath(os.readlink(f"/proc/{os.getpid()}/exe")))
        expected_command_line = tuple(
            os.fsdecode(argument)
            for argument in Path(f"/proc/{os.getpid()}/cmdline")
            .read_bytes()
            .rstrip(b"\0")
            .split(b"\0")
            if argument
        )
        self.assertEqual(identity.cmdline, expected_command_line)
        self.assertGreater(identity.start_time, 0)

    def test_ownership_record_is_atomic_private_and_round_trips_exactly(self) -> None:
        record = self.ownership()

        self.adapter._write_ownership(record)

        self.assertEqual(self.adapter._read_ownership(), record)
        self.assertEqual(stat.S_IMODE(self.adapter._ownership_path.stat().st_mode), 0o600)

    def test_new_record_binds_server_to_exact_launch_identity(self) -> None:
        owner = self.ownership().owner
        command = self.adapter._server_command()
        with (
            patch.object(stt, "_process_identity", return_value=owner),
            patch.object(stt, "_process_start_time", return_value=303),
            patch.object(stt, "_current_boot_id", return_value="current-boot"),
        ):
            record = self.adapter._new_ownership(23003, command)

        self.assertEqual(record.boot_id, "current-boot")
        self.assertEqual(record.owner, owner)
        self.assertEqual(record.server.pid, 23003)
        self.assertEqual(record.server.uid, os.getuid())
        self.assertEqual(record.server.exe, str(self.server_binary.resolve()))
        self.assertEqual(record.server.cmdline, tuple(command))
        self.assertEqual(record.server.start_time, 303)

    async def test_exact_orphan_identity_is_terminated_through_its_pidfd(self) -> None:
        record = self.ownership()
        wait_for_exit = AsyncMock(return_value=True)
        with (
            patch.object(self.adapter, "_read_ownership", return_value=record),
            patch.object(self.adapter, "_discard_ownership") as discard,
            patch.object(self.adapter, "_wait_for_recorded_exit", wait_for_exit),
            patch.object(stt, "_current_boot_id", return_value=record.boot_id),
            patch.object(stt, "_same_process_alive", side_effect=[True, False]),
            patch.object(stt, "_process_identity", return_value=record.server),
            patch.object(stt.os, "pidfd_open", return_value=73) as pidfd_open,
            patch.object(stt.signal, "pidfd_send_signal") as send_signal,
            patch.object(stt.os, "close") as close_fd,
        ):
            reclaimed = await self.adapter._reclaim_stale_server()

        self.assertTrue(reclaimed)
        pidfd_open.assert_called_once_with(record.server.pid, 0)
        send_signal.assert_called_once_with(73, signal.SIGTERM, None, 0)
        wait_for_exit.assert_awaited_once_with(record.server, 0.1)
        close_fd.assert_called_once_with(73)
        discard.assert_called_once_with(record)

    async def test_any_live_server_identity_mismatch_prevents_all_signals(self) -> None:
        record = self.ownership()
        mismatches = {
            "uid": replace(record.server, uid=record.server.uid + 1),
            "exe": replace(record.server, exe="/tmp/not-whisper-server"),
            "cmdline": replace(record.server, cmdline=("whisper-server", "--other")),
            "start_time": replace(record.server, start_time=record.server.start_time + 1),
        }
        for field, actual in mismatches.items():
            with self.subTest(field=field):
                with (
                    patch.object(self.adapter, "_read_ownership", return_value=record),
                    patch.object(stt, "_current_boot_id", return_value=record.boot_id),
                    patch.object(stt, "_same_process_alive", return_value=True),
                    patch.object(stt, "_process_identity", return_value=actual),
                    patch.object(stt.os, "pidfd_open", return_value=74),
                    patch.object(stt.signal, "pidfd_send_signal") as send_signal,
                    patch.object(stt.os, "close"),
                ):
                    with self.assertRaisesRegex(RuntimeError, "identity no longer matches"):
                        await self.adapter._reclaim_stale_server()
                send_signal.assert_not_called()

    async def test_live_recorded_owner_prevents_all_signals(self) -> None:
        record = self.ownership()
        with (
            patch.object(self.adapter, "_read_ownership", return_value=record),
            patch.object(stt, "_current_boot_id", return_value=record.boot_id),
            patch.object(stt, "_same_process_alive", side_effect=[True, True]),
            patch.object(stt, "_process_identity", side_effect=[record.server, record.owner]),
            patch.object(stt.os, "pidfd_open", return_value=75),
            patch.object(stt.signal, "pidfd_send_signal") as send_signal,
            patch.object(stt.os, "close"),
        ):
            with self.assertRaisesRegex(RuntimeError, "still owned by a live E.V. core"):
                await self.adapter._reclaim_stale_server()

        send_signal.assert_not_called()

    async def test_reused_or_exited_recorded_pid_is_only_forgotten(self) -> None:
        record = self.ownership()
        with (
            patch.object(self.adapter, "_read_ownership", return_value=record),
            patch.object(self.adapter, "_discard_ownership") as discard,
            patch.object(stt, "_current_boot_id", return_value=record.boot_id),
            patch.object(stt, "_same_process_alive", return_value=False),
            patch.object(stt.os, "pidfd_open") as pidfd_open,
            patch.object(stt.signal, "pidfd_send_signal") as send_signal,
        ):
            reclaimed = await self.adapter._reclaim_stale_server()

        self.assertFalse(reclaimed)
        discard.assert_called_once_with(record)
        pidfd_open.assert_not_called()
        send_signal.assert_not_called()

    async def test_unrecorded_process_on_server_port_is_never_touched(self) -> None:
        with (
            patch.object(self.adapter, "_reclaim_stale_server", new=AsyncMock(return_value=False)),
            patch.object(self.adapter, "_port_ready", new=AsyncMock(return_value=True)),
            patch.object(stt.asyncio, "create_subprocess_exec", new=AsyncMock()) as spawn,
            patch.object(stt.signal, "pidfd_send_signal") as send_signal,
        ):
            with self.assertRaisesRegex(RuntimeError, "port is already occupied"):
                await self.adapter._ensure_server()

        spawn.assert_not_awaited()
        send_signal.assert_not_called()


if __name__ == "__main__":
    unittest.main()
