from __future__ import annotations

import os
import signal
import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import psutil

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "core"))

from ev.tools.builtin import close_process, get_processes
from ev.tools.base import ValidationError


class ListedProcess:
    def __init__(self, *, pid: int, name: str, exe: str, cmdline: list[str]) -> None:
        self.info = {
            "pid": pid,
            "name": name,
            "username": "phax",
            "memory_info": SimpleNamespace(rss=4096),
            "create_time": 42.0,
            "exe": exe,
            "cmdline": cmdline,
        }

    def cpu_percent(self, interval=None):
        return 0.0

    def ppid(self):
        return 1


class CloseTarget:
    def __init__(
        self,
        *,
        pid: int = 77,
        name: str,
        exe: str,
        cmdline: list[str],
        wait_results: list[str] | None = None,
    ) -> None:
        self.pid = pid
        self._name = name
        self._exe = exe
        self._cmdline = cmdline
        self._started = 42.0
        self._alive = True
        self.wait_results = list(wait_results or [])
        self.signals: list[int] = []

    def oneshot(self):
        return nullcontext()

    def name(self):
        return self._name

    def username(self):
        return "phax"

    def create_time(self):
        return self._started

    def exe(self):
        return self._exe

    def cmdline(self):
        return list(self._cmdline)

    def uids(self):
        return SimpleNamespace(real=os.getuid())

    def send_signal(self, sent_signal):
        self.signals.append(sent_signal)

    def wait(self, timeout=None):
        result = self.wait_results.pop(0) if self.wait_results else "timeout"
        if result == "exit":
            self._alive = False
            return 0
        raise psutil.TimeoutExpired(timeout or 0, pid=self.pid, name=self._name)

    def is_running(self):
        return self._alive

    def status(self):
        return psutil.STATUS_RUNNING if self._alive else psutil.STATUS_ZOMBIE


class ApplicationCloseSafetyTests(unittest.TestCase):
    def test_interpreter_script_argv_matches_without_being_exposed(self) -> None:
        process = ListedProcess(
            pid=257975,
            name="python3",
            exe="/usr/bin/python3.14",
            cmdline=[
                "/usr/lib/python-exec/python3.14/python3",
                "/home/test-user/.local/bin/phaxity-neko-music",
            ],
        )
        with (
            patch("ev.tools.builtin.psutil.process_iter", return_value=[process]),
            patch("ev.tools.builtin.time.sleep"),
        ):
            result = get_processes(
                {"query": "phaxity-neko-music", "sort": "memory", "limit": 5},
                None,
            )

        self.assertEqual([row["pid"] for row in result["processes"]], [257975])
        self.assertNotIn("cmdline", result["processes"][0])
        self.assertNotIn("exe", result["processes"][0])

    def test_expected_query_must_still_match_the_exact_pid(self) -> None:
        target = CloseTarget(
            name="firefox-bin",
            exe="/usr/lib64/firefox/firefox-bin",
            cmdline=["/usr/lib64/firefox/firefox-bin"],
        )
        with patch("ev.tools.builtin.psutil.Process", return_value=target):
            with self.assertRaisesRegex(ValidationError, "no longer matches"):
                close_process(
                    {"pid": target.pid, "started_at_epoch": 42.0, "expected_query": "spotify"},
                    None,
                )

        self.assertEqual(target.signals, [])

    def test_python_script_target_is_revalidated_then_closed(self) -> None:
        target = CloseTarget(
            name="python3",
            exe="/usr/bin/python3.14",
            cmdline=["/usr/bin/python3", "/home/test-user/.local/bin/phaxity-neko-music"],
            wait_results=["exit"],
        )
        with patch("ev.tools.builtin.psutil.Process", return_value=target):
            result = close_process(
                {
                    "pid": target.pid,
                    "started_at_epoch": 42.0,
                    "expected_query": "phaxity-neko-music",
                },
                None,
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["closed"])
        self.assertEqual(result["method"], "sigterm")
        self.assertEqual(target.signals, [signal.SIGTERM])

    def test_spotify_prefers_mpris_quit_and_verifies_exit(self) -> None:
        target = CloseTarget(
            name="spotify",
            exe="/app/extra/share/spotify/spotify",
            cmdline=["/app/extra/share/spotify/spotify"],
            wait_results=["exit"],
        )
        command_results = [
            {"ok": True, "stdout": "true\n"},
            {"ok": True, "stdout": ""},
        ]
        with (
            patch("ev.tools.builtin.psutil.Process", return_value=target),
            patch("ev.tools.builtin.run_command", side_effect=command_results) as run,
        ):
            result = close_process(
                {"pid": target.pid, "started_at_epoch": 42.0, "expected_query": "spotify"},
                None,
            )

        self.assertTrue(result["closed"])
        self.assertEqual(result["method"], "mpris_quit")
        self.assertEqual(target.signals, [])
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[1].args[0][-1], "org.mpris.MediaPlayer2.Quit")

    def test_spotify_uses_app_scoped_fallback_after_graceful_close_fails(self) -> None:
        target = CloseTarget(
            name="spotify",
            exe="/app/extra/share/spotify/spotify",
            cmdline=["/app/extra/share/spotify/spotify"],
            wait_results=["timeout", "exit"],
        )
        command_results = [
            {"ok": False, "stdout": ""},
            {"ok": True, "stdout": ""},
        ]
        with (
            patch("ev.tools.builtin.psutil.Process", return_value=target),
            patch("ev.tools.builtin.run_command", side_effect=command_results) as run,
        ):
            result = close_process(
                {"pid": target.pid, "started_at_epoch": 42.0, "expected_query": "spotify"},
                None,
            )

        self.assertTrue(result["closed"])
        self.assertEqual(result["method"], "flatpak_kill")
        self.assertEqual(target.signals, [signal.SIGTERM])
        self.assertEqual(
            run.call_args_list[-1].args[0], ["/usr/bin/flatpak", "kill", "com.spotify.Client"]
        )

    def test_unresponsive_process_is_not_reported_as_success(self) -> None:
        target = CloseTarget(
            name="example-app",
            exe="/opt/example/example-app",
            cmdline=["/opt/example/example-app"],
            wait_results=["timeout"],
        )
        with patch("ev.tools.builtin.psutil.Process", return_value=target):
            result = close_process(
                {"pid": target.pid, "started_at_epoch": 42.0, "expected_query": "example app"},
                None,
            )

        self.assertFalse(result["ok"])
        self.assertFalse(result["closed"])
        self.assertEqual(result["status"], "still_running")
        self.assertEqual(result["method"], "none")


if __name__ == "__main__":
    unittest.main()
