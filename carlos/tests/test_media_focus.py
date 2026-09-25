from __future__ import annotations
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from ev.voice.media_focus import MediaFocus


class MediaFocusTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_mute_survives_conversation_restore_for_exact_stream(self):
        await self.focus.pause()
        identity = list(self.focus.streams["10"])
        await self.focus.preserve_explicit_mute("10", identity)
        await self.focus.restore()
        self.assertTrue(self.rows[0]["mute"])

    async def test_wrong_stream_identity_does_not_drop_restore_record(self):
        await self.focus.pause()
        await self.focus.preserve_explicit_mute("10", ["other"])
        self.assertIn("10", self.focus.streams)

    def setUp(self):
        self.rows = [
            {
                "index": 10,
                "client": 4,
                "sink": 1,
                "corked": False,
                "mute": False,
                "properties": {"application.name": "Firefox"},
            },
            {
                "index": 11,
                "client": 5,
                "corked": False,
                "mute": True,
                "properties": {"application.name": "Muted app"},
            },
            {
                "index": 12,
                "client": 6,
                "corked": False,
                "mute": False,
                "properties": {"application.name": "E.V."},
            },
        ]
        self.focus = MediaFocus(AsyncMock(side_effect=lambda _: self.rows), dict)
        self.status = "Playing"
        self.metadata = "track-one"
        self.actions = []

        async def run(*args):
            if args == ("/usr/bin/qdbus6",):
                return "org.mpris.MediaPlayer2.test"
            if args[0] == "/usr/bin/pactl":
                self.rows[0]["mute"] = args[-1] == "1"
            return ""

        async def control(owner, action):
            self.actions.append((owner, action))
            self.status = "Paused" if action == "Pause" else "Playing"

        self.focus._run = AsyncMock(side_effect=run)
        self.focus._owner = AsyncMock(return_value=":1.123")
        self.focus._property = AsyncMock(
            side_effect=lambda owner, key: self.status if key == "PlaybackStatus" else self.metadata
        )
        self.focus._control = AsyncMock(side_effect=control)

    async def test_pause_resume_preserves_other_streams_and_exact_player(self):
        await self.focus.pause()
        await self.focus.pause()
        self.assertEqual(self.actions, [(":1.123", "Pause")])
        self.assertEqual(set(self.focus.streams), {"10"})
        self.assertTrue(self.rows[0]["mute"])
        await self.focus.restore()
        self.assertEqual(self.actions[-1], (":1.123", "Play"))
        self.assertFalse(self.rows[0]["mute"])
        self.assertTrue(self.rows[1]["mute"])
        self.assertFalse(self.focus.active)

    async def test_previously_paused_player_never_starts(self):
        self.status = "Paused"
        await self.focus.pause()
        await self.focus.restore()
        self.assertEqual(self.actions, [])

    async def test_changed_track_is_not_resumed(self):
        await self.focus.pause()
        self.metadata = "different-track"
        await self.focus.restore()
        self.assertEqual(self.actions, [(":1.123", "Pause")])

    async def test_replacement_player_is_not_resumed(self):
        await self.focus.pause()
        self.focus._owner.return_value = ":1.999"
        await self.focus.restore()
        self.assertEqual(self.actions, [(":1.123", "Pause")])

    async def test_reused_stream_index_is_not_unmuted(self):
        await self.focus.pause()
        self.rows[0]["client"] = 999
        await self.focus.restore()
        self.assertTrue(self.rows[0]["mute"])

    async def test_audio_service_failure_retains_restoration_records(self):
        await self.focus.pause()
        listing = self.focus.listing
        self.focus.listing = AsyncMock(side_effect=OSError("disconnected"))
        await self.focus.restore()
        self.assertTrue(self.focus.active)
        self.assertIn("10", self.focus.streams)
        self.focus.listing = listing
        await self.focus.restore()
        self.assertFalse(self.focus.active)

    async def test_explicit_control_override_does_not_resume(self):
        await self.focus.pause()
        await self.focus.restore(resume=False)
        self.assertFalse(self.rows[0]["mute"])
        self.assertEqual(self.actions, [(":1.123", "Pause")])
