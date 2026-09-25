from __future__ import annotations

import asyncio
import sys
import subprocess
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from ev.tools.builtin import (
    _build_application_catalog,
    _entry_identity_keys,
    _process_matches_query,
    _process_query_token_sets,
    list_applications,
    open_application,
)


class ApplicationIdentityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.entry = {
            "desktop_id": "com.phaxity.NekoMusic",
            "name": "Phaxity Audio",
            "generic_name": "Music player",
            "_exec_identities": ["python3", "phaxity-neko-music"],
        }

    def test_gui_launch_never_waits_for_inherited_output_pipes(self):
        with patch("ev.tools.builtin.desktop_entries", return_value={"firefox": {}}), patch(
            "ev.tools.builtin.subprocess.run", return_value=SimpleNamespace(returncode=0)
        ) as run:
            result = open_application({"desktop_id": "firefox"}, None)
        self.assertTrue(result["launched"])
        self.assertEqual(run.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertTrue(run.call_args.kwargs["start_new_session"])
        self.assertEqual(result["verification"], "launcher_acceptance_only")

    def test_launcher_failure_is_not_success(self):
        with patch("ev.tools.builtin.desktop_entries", return_value={"firefox": {}}), patch(
            "ev.tools.builtin.subprocess.run", return_value=SimpleNamespace(returncode=1)
        ):
            self.assertFalse(open_application({"desktop_id": "firefox"}, None)["launched"])

    def test_launcher_timeout_is_uncertain_and_not_retried(self):
        with patch("ev.tools.builtin.desktop_entries", return_value={"firefox": {}}), patch(
            "ev.tools.builtin.subprocess.run",
            side_effect=subprocess.TimeoutExpired("gtk-launch", 3),
        ) as run:
            result = open_application({"desktop_id": "firefox"}, None)
        self.assertFalse(result["launched"])
        self.assertEqual(result["activation_status"], "unverified")
        self.assertIn("may still have opened", result["detail"])
        self.assertEqual(run.call_count, 1)

    async def test_catalog_does_not_block_audio_loop_during_cold_scan(self) -> None:
        def slow_scan():
            time.sleep(0.08)
            return {"neko": dict(self.entry)}

        with patch("ev.tools.builtin.desktop_entries", side_effect=slow_scan), patch(
            "ev.tools.builtin._running_process_keys", return_value={}
        ):
            task = asyncio.create_task(list_applications({"query": "neko"}, None))
            await asyncio.sleep(0.01)
            self.assertFalse(task.done(), "Desktop scan blocked the event loop")
            result = await task
        self.assertEqual(result["applications"][0]["desktop_id"], "com.phaxity.NekoMusic")

    async def test_launch_lookup_does_not_wait_for_kwin_or_processes(self) -> None:
        desktop = SimpleNamespace(
            snapshot=AsyncMock(
                side_effect=AssertionError("Must not scan windows to open an installed app")
            )
        )
        context = SimpleNamespace(desktop=desktop)
        with patch("ev.tools.builtin.desktop_entries", return_value={"neko": self.entry}), patch(
            "ev.tools.builtin._running_process_keys",
            side_effect=AssertionError("Must not scan processes"),
        ):
            result = await list_applications({"query": "neko", "launch_only": True}, context)
        self.assertEqual(result["applications"][0]["desktop_id"], "com.phaxity.NekoMusic")
        self.assertEqual(result["sources"], ["desktop_entries"])
        self.assertNotIn("running", result["applications"][0])
        desktop.snapshot.assert_not_awaited()

    async def test_shared_interpreter_cannot_associate_or_close_other_python_apps(self) -> None:
        self.assertNotIn("python3", _entry_identity_keys(self.entry))
        self.assertNotIn("music player", _entry_identity_keys(self.entry))
        windows = [
            {
                "id": "unrelated",
                "resource_class": "python3",
                "title": "Phaxity Audio documentation",
                "pid": 52,
            }
        ]
        result = _build_application_catalog(
            [dict(self.entry)], {"python3": {52}}, windows, "neko", 8
        )
        self.assertFalse(result["applications"][0]["running"])
        with patch("ev.tools.builtin.desktop_entries", return_value={"neko": self.entry}):
            token_sets = _process_query_token_sets("phaxity neko music")
        unrelated = {
            "name": "python3",
            "exe": "/usr/bin/python3",
            "cmdline": ["python3", "/tmp/unrelated.py"],
        }
        self.assertFalse(_process_matches_query(unrelated, "phaxity neko music", token_sets))

    async def test_shared_flatpak_launcher_is_not_spotify_identity(self) -> None:
        entry = {
            "desktop_id": "com.spotify.Client",
            "name": "Spotify",
            "_exec_identities": ["flatpak", "com.spotify.Client"],
        }
        self.assertNotIn("flatpak", _entry_identity_keys(entry))
        result = _build_application_catalog([entry], {"flatpak": {88}}, [], "spotify", 8)
        self.assertFalse(result["applications"][0]["running"])
