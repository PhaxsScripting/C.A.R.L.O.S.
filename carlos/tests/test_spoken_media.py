from __future__ import annotations

import unittest
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, PropertyMock, patch, Mock

from ev.commands import direct_action, media_request, request_text
from ev.tools.media import control_media
from ev.voice.media_focus import MediaFocus
from ev.voice.normalization import normalize_transcript, interpret_spoken_command
from ev.voice.stt import Transcript, WhisperCppAdapter
from ev.service import CarlosCore
from ev.paths import Paths
from ev.state import CoreState
from ev.intents import extract_close_targets, split_action_clauses


class SpokenMediaTests(unittest.TestCase):
    def test_casual_media_and_stop_do_not_trigger_slow_close_verification(self):
        for phrase in (
            "stop my Spotify, bro, what the fuck",
            "stop talking",
            "stop",
            "pause Spotify",
            "unpause my Spotify bro",
        ):
            self.assertEqual(extract_close_targets(phrase), (), phrase)
        self.assertEqual(extract_close_targets("close Firefox, bro"), ("firefox",))
        self.assertEqual(len(split_action_clauses("lower the volume and unpause Spotify")), 2)

    def test_confirmed_transcripts_select_expected_transport(self):
        for raw, action, player in (
            ("Plause, spot for it!", "pause", "spotify"),
            ("Pause, spawn for it.", "pause", "spotify"),
            ("Plasma Music.", "pause", None),
            ("E.V., Unpause my Spotify, bro, what the fuck.", "play", "spotify"),
            ("Could you resume my music please?", "play", None),
            ("play Spotify bro", "play", "spotify"),
            ("skip to the next song on Spotify", "next", "spotify"),
        ):
            with self.subTest(raw=raw):
                interpreted = interpret_spoken_command(normalize_transcript(raw), media_active=True)
                selected = direct_action(interpreted.text)
                self.assertIsNotNone(selected)
                self.assertEqual(selected.tool, "audio.media")
                self.assertEqual(selected.arguments.get("action"), action)
                self.assertEqual(selected.arguments.get("player"), player)

    def test_discussion_negation_titles_and_literal_input_are_not_repaired(self):
        for phrase in (
            "What is Plasma Music?",
            "open Plasma Music",
            "don't pause Spotify",
            "type pause spawn for it",
            'play the song "Plasma Music"',
            'say "plause spot for it"',
            "I think pause my music sounds better",
            "pause Spotify if the song ends",
            "pause Spotify, actually don't",
            "unpause Spotify, but don't do it yet",
            "what should I learn next",
        ):
            with self.subTest(phrase=phrase):
                self.assertEqual(interpret_spoken_command(phrase, media_active=True).text, phrase)
                self.assertIsNone(media_request(phrase))
        for phrase in (
            "type hello bro",
            "search for hello bro",
            'open "hello bro"',
            "take a note saying pause spotify bro",
        ):
            self.assertEqual(request_text(phrase), phrase)

    def test_uncertain_short_media_fragment_gets_fast_clarification(self):
        result = interpret_spoken_command("Plasma Music.")
        self.assertTrue(result.clarification)
        self.assertEqual(result.text, "Plasma Music.")

    def test_other_direct_commands_allow_trailing_asides(self):
        for phrase, tool in (
            ("set volume to 40 percent, bro, what the fuck", "audio.set_volume"),
            ("minimize my window, dude", "window.state"),
        ):
            self.assertEqual(direct_action(phrase).tool, tool)


class MediaTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.players = {
            "org.mpris.MediaPlayer2.spotify": {
                "owner": ":1.20",
                "status": "Paused",
                "metadata": "spotify-track-1",
            },
            "org.mpris.MediaPlayer2.firefox.instance_1": {
                "owner": ":1.30",
                "status": "Paused",
                "metadata": "video-1",
            },
        }
        self.focus = MediaFocus(AsyncMock(return_value=[]), dict)
        self.context = SimpleNamespace(media_focus=self.focus)
        self.focus._run = AsyncMock(side_effect=lambda *_: "\n".join(self.players))
        self.focus._owner = AsyncMock(side_effect=lambda service: self.players[service]["owner"])

        def property_value(owner, key):
            row = next(row for row in self.players.values() if row["owner"] == owner)
            return row["status"] if key == "PlaybackStatus" else row["metadata"]

        async def control(owner, method):
            row = next(row for row in self.players.values() if row["owner"] == owner)
            if method in {"Play", "Pause"}:
                row["status"] = "Playing" if method == "Play" else "Paused"
            else:
                row["metadata"] += "-next"

        self.focus._property = AsyncMock(side_effect=property_value)
        self.focus._control = AsyncMock(side_effect=control)

    async def test_spotify_targets_unique_owner_and_verifies_result(self):
        result = await control_media({"action": "play", "player": "spotify"}, self.context)
        self.assertTrue(result["verified"])
        self.focus._control.assert_awaited_once_with(":1.20", "Play")
        self.assertEqual(
            self.players["org.mpris.MediaPlayer2.firefox.instance_1"]["status"], "Paused"
        )

    async def test_explicit_pause_keeps_other_players_restore_records(self):
        self.focus.players = {
            name: (row["owner"], row["metadata"]) for name, row in self.players.items()
        }
        self.focus.active = True
        result = await control_media({"action": "pause", "player": "spotify"}, self.context)
        self.assertTrue(result["verified"])
        self.assertNotIn("org.mpris.MediaPlayer2.spotify", self.focus.players)
        await self.focus.restore()
        self.focus._control.assert_awaited_once_with(":1.30", "Play")

    async def test_generic_pause_finds_player_held_by_wake(self):
        self.focus.players["org.mpris.MediaPlayer2.spotify"] = (":1.20", "spotify-track-1")
        result = await control_media({"action": "pause"}, self.context)
        self.assertTrue(result["verified"])
        self.assertEqual(result["player"], "org.mpris.MediaPlayer2.spotify")

    async def test_ambiguous_players_do_not_trigger_any_control(self):
        result = await control_media({"action": "play"}, self.context)
        self.assertFalse(result["verified"])
        self.focus._control.assert_not_awaited()

    async def test_missing_spotify_never_pauses_firefox(self):
        self.players.pop("org.mpris.MediaPlayer2.spotify")
        result = await control_media({"action": "pause", "player": "spotify"}, self.context)
        self.assertFalse(result["verified"])
        self.focus._control.assert_not_awaited()

    async def test_held_next_track_restores_new_track_at_conversation_end(self):
        self.focus.players["org.mpris.MediaPlayer2.spotify"] = (":1.20", "spotify-track-1")
        self.focus.active = True
        result = await control_media({"action": "next", "player": "spotify"}, self.context)
        self.assertTrue(result["verified"])
        await self.focus.restore()
        self.assertEqual(self.players["org.mpris.MediaPlayer2.spotify"]["status"], "Playing")

    async def test_rejected_control_does_not_claim_success_or_repeat(self):
        self.focus._control = AsyncMock()
        result = await control_media({"action": "play", "player": "spotify"}, self.context)
        self.assertFalse(result["verified"])
        self.focus._control.assert_awaited_once()

    async def test_replacement_owner_is_not_controlled(self):
        self.focus._owner = AsyncMock(side_effect=[":1.20", ":1.99"])
        result = await control_media({"action": "play", "player": "spotify"}, self.context)
        self.assertFalse(result["verified"])
        self.focus._control.assert_not_awaited()

    async def test_owner_replaced_after_dispatch_is_not_reported_as_verified(self):
        self.focus._owner = AsyncMock(side_effect=[":1.20", ":1.20", ":1.99"])
        result = await control_media({"action": "play", "player": "spotify"}, self.context)
        self.assertFalse(result["verified"])
        self.assertTrue(result["command_sent"])
        self.focus._control.assert_awaited_once_with(":1.20", "Play")
        self.assertIn("replaced", result["message"])

    async def test_full_voice_command_uses_transport_without_language_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = CarlosCore(
                paths=Paths(
                    *(root / name for name in ("config", "data", "state", "cache", "runtime"))
                )
            )
            service.brain.submit = AsyncMock(
                side_effect=AssertionError("Media request went to the language model")
            )
            manager = service.voice
            manager.media_focus = self.focus
            service.tools.context.media_focus = self.focus
            manager.response_handler = Mock()
            manager.stt.transcribe = AsyncMock(
                return_value=Transcript("Plause, spot for it!", "test", "test", 1)
            )
            self.players["org.mpris.MediaPlayer2.spotify"]["status"] = "Playing"
            try:
                manager.capture_active = True
                manager.capture_origin = "ambient"
                manager.capture_mode = "wake_command"
                manager.capture_started = time.monotonic()
                manager.capture_bytes = 16000
                manager.capture_pcm.extend(b"\x10\x10" * 8000)
                manager.capture_speech_start_byte = 0
                manager.diagnostics.update({"max_rms": 0.05, "max_peak": 0.2})
                service.state.transition(CoreState.LISTENING, "test")
                with patch.object(
                    WhisperCppAdapter,
                    "available",
                    new_callable=PropertyMock,
                    return_value=(True, "test"),
                ):
                    result = await manager.stop_capture("speech-regression")
                self.assertEqual(result["command"]["status"], "completed")
                self.assertEqual(result["raw_transcript"], "Plause, spot for it!")
                self.assertEqual(result["normalized_transcript"], "pause Spotify")
                self.assertEqual(result["command"]["response"], "Spotify is paused.")
                service.brain.submit.assert_not_awaited()
                self.focus._control.assert_awaited_once_with(":1.20", "Pause")
                self.assertFalse(manager.capture_pcm)
            finally:
                await manager.close()
                service.memory.close()
