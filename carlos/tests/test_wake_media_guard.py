from __future__ import annotations
import asyncio
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from ev.events import PhaxEventBus
from ev.state import StateMachine
from ev.voice.manager import VoiceManager


class WakeMediaGuardTests(unittest.IsolatedAsyncioTestCase):
    def manager(self):
        bus = PhaxEventBus()
        return VoiceManager(
            {
                "wake": {
                    "enabled": True,
                    "media_requires_hey": True,
                    "candidate_cooldown_seconds": 1.2,
                }
            },
            bus,
            StateMachine(bus),
        )

    async def test_bare_alias_during_media_is_filtered_without_capture(self):
        manager = self.manager()
        manager._media_output_active = AsyncMock(return_value=True)
        await manager._on_wake_detected({"keyword": "EVE"})
        self.assertFalse(manager.capture_active)
        self.assertEqual(manager.diagnostics["wake_guard"], "MEDIA_REQUIRES_HEY")

    async def test_explicit_hey_works_during_media_without_extra_probe(self):
        manager = self.manager()
        manager._media_output_active = AsyncMock(return_value=True)
        await manager._on_wake_detected({"keyword": "HEY_E_V"})
        self.assertTrue(manager.capture_active)
        manager._media_output_active.assert_not_awaited()
        await manager.abort_capture("test")

    async def test_bare_name_still_works_when_media_is_not_playing(self):
        manager = self.manager()
        manager._media_output_active = AsyncMock(return_value=False)
        await manager._on_wake_detected({"keyword": "E_V"})
        self.assertTrue(manager.capture_active)
        await manager.abort_capture("test")

    async def test_repeated_candidates_are_debounced(self):
        manager = self.manager()
        manager._media_output_active = AsyncMock(return_value=True)
        await manager._on_wake_detected({"keyword": "EVE"})
        await manager._on_wake_detected({"keyword": "EVE"})
        self.assertEqual(manager._media_output_active.await_count, 1)

    async def test_headphone_playback_does_not_require_hey(self):
        manager = self.manager()
        manager._audio_listing = AsyncMock(
            side_effect=[
                [{"sink": 2, "corked": False, "mute": False}],
                [{"index": 2, "active_port": "headphone-output"}],
            ]
        )
        self.assertFalse(await manager._media_output_active())

    async def test_any_speaker_stream_retains_movie_guard(self):
        manager = self.manager()
        manager._audio_listing = AsyncMock(
            side_effect=[
                [{"sink": 2, "corked": False}, {"sink": 3, "corked": False}],
                [
                    {"index": 2, "active_port": "headphone-output"},
                    {"index": 3, "active_port": "analog-output-speaker"},
                ],
            ]
        )
        self.assertTrue(await manager._media_output_active())

    def test_active_speaker_port_overrides_headset_capability(self):
        self.assertFalse(
            VoiceManager._private_audio_sink(
                {
                    "active_port": "analog-output-speaker",
                    "properties": {"device.form_factor": "headset"},
                }
            )
        )

    async def test_media_pause_mode_accepts_bare_name_and_releases_on_stop(self):
        manager = self.manager()
        manager.config["wake"]["pause_media_on_wake"] = True
        manager._media_output_active = AsyncMock(return_value=True)
        manager.media_focus.pause = AsyncMock()
        manager.media_focus.restore = AsyncMock()
        await manager._on_wake_detected({"keyword": "EVIE"})
        self.assertTrue(manager.capture_active)
        manager.media_focus.pause.assert_awaited_once()
        manager._media_output_active.assert_not_awaited()
        await manager.end_conversation()
        manager.media_focus.restore.assert_awaited()
        self.assertFalse(manager.capture_active)

    async def test_idle_watcher_restores_after_conversation(self):
        manager = self.manager()
        manager.media_focus.active = True
        manager.media_focus.restore = AsyncMock(
            side_effect=lambda **kw: setattr(manager.media_focus, "active", False)
        )
        await asyncio.wait_for(manager._watch_media_focus(), 2)
        manager.media_focus.restore.assert_awaited_once()

    async def test_explicit_playback_override_drops_automatic_resume(self):
        manager = self.manager()
        manager.media_focus.players["org.mpris.MediaPlayer2.test"] = (":1.123", "song")
        manager.media_focus.restore = AsyncMock()
        await manager.release_media_focus(resume=False)
        self.assertEqual(manager.media_focus.players, {})
        manager.media_focus.restore.assert_awaited_once_with(resume=False)

    async def test_movie_cannot_supply_an_automatic_follow_up(self):
        manager = self.manager()
        manager._media_output_active = AsyncMock(return_value=True)
        manager.start_capture = AsyncMock()
        await manager._start_follow_up()
        manager.start_capture.assert_not_awaited()
        self.assertEqual(manager.diagnostics["follow_up_state"], "WAITING_FOR_HEY")

    async def test_normal_conversation_keeps_automatic_follow_up(self):
        manager = self.manager()
        manager._media_output_active = AsyncMock(return_value=False)
        manager.start_capture = AsyncMock()
        await manager._start_follow_up()
        manager.start_capture.assert_awaited_once()
        self.assertEqual(manager.start_capture.call_args.args[1], "follow_up")

    async def test_own_speech_muted_and_paused_streams_are_not_movie_audio(self):
        manager = self.manager()
        streams = [
            {"corked": False, "mute": False, "properties": {"application.name": "E.V."}},
            {"corked": True, "mute": False, "properties": {"application.name": "Spotify"}},
            {"corked": False, "mute": True, "properties": {"application.name": "Firefox"}},
        ]
        process = AsyncMock()
        process.returncode = 0
        process.communicate.return_value = (json.dumps(streams).encode(), b"")
        with patch(
            "ev.voice.manager.asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)
        ):
            self.assertFalse(await manager._media_output_active())
            manager._media_check_at = 0
            streams.append(
                {"corked": False, "mute": False, "properties": {"application.name": "Spotify"}}
            )
            process.communicate.return_value = (json.dumps(streams).encode(), b"")
            self.assertTrue(await manager._media_output_active())
