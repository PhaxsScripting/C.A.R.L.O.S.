from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "core"))

from ev.ai import OfflineProvider  # noqa: E402
from ev.events import PhaxEventBus  # noqa: E402
from ev.intents import extract_close_targets, split_action_clauses  # noqa: E402
from ev.paths import Paths  # noqa: E402
from ev.service import CarlosCore  # noqa: E402
from ev.state import CoreState, StateMachine  # noqa: E402
from ev.tools.builtin import mpris  # noqa: E402
from ev.voice import Transcript, VoiceManager, normalize_transcript  # noqa: E402


class IntentReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_natural_polite_wrappers_keep_the_fast_execution_route(self):
        for text in (
            "Can you just open Firefox?",
            "Actually, open Firefox",
            "Please just open Firefox",
            "Go ahead and open Firefox",
            "Do me a favor and open Firefox",
        ):
            turn = await OfflineProvider().begin(text, [], [], [])
            self.assertEqual(turn.tool_calls[0].name, "applications.list", text)
            self.assertEqual(turn.tool_calls[0].arguments["query"], "firefox", text)

    def test_polite_wrappers_preserve_discussion_and_negation(self):
        from ev.commands import request_text

        for text in (
            "Can you just help me create cybersecurity tools?",
            "Just don't open Firefox",
            "Please explain how to open Firefox",
            "Actually how do I shut down my PC?",
        ):
            self.assertIsNone(request_text(text), text)

    def test_compound_actions_split_only_at_real_action_boundaries(self) -> None:
        self.assertEqual(
            split_action_clauses("Open Firefox, Ben, and close Spotify."),
            ["Open Firefox", "close Spotify"],
        )
        self.assertEqual(
            split_action_clauses("open Firefox and close Spotify"),
            ["open Firefox", "close Spotify"],
        )
        self.assertEqual(
            split_action_clauses("play rock and roll music"),
            ["play rock and roll music"],
        )
        self.assertEqual(
            split_action_clauses("what is black and white?"),
            ["what is black and white?"],
        )

    def test_compound_action_splitter_never_executes_quoted_payload(self) -> None:
        text = 'open Firefox then type "hello and close Spotify" in Firefox'
        self.assertEqual(
            split_action_clauses(text),
            ["open Firefox", 'type "hello and close Spotify" in Firefox'],
        )
        self.assertEqual(
            split_action_clauses('type "hello and close Spotify" in Firefox'),
            ['type "hello and close Spotify" in Firefox'],
        )

    async def test_shared_close_verb_resolves_two_exact_app_targets(self):
        for phrase in (
            "close spotify and my terminal window",
            "close Spotify and my terminal windows",
            "please close Spotify and my terminal window",
        ):
            clauses = split_action_clauses(phrase)
            self.assertEqual(len(clauses), 2)
            self.assertEqual(extract_close_targets(phrase), ("spotify", "konsole"))
            queries = []
            for clause in clauses:
                turn = await OfflineProvider().begin(clause, [], [], [])
                self.assertEqual(turn.tool_calls[0].name, "system.get_processes")
                queries.append(turn.tool_calls[0].arguments["query"])
            self.assertEqual(queries, ["spotify", "konsole"])

    def test_shared_verb_keeps_ambiguous_or_quoted_targets_intact(self):
        for phrase in (
            'close "Spotify and terminal"',
            "close Spotify and don't close terminal",
            "close Spotify and some unknown app",
            "explain how to close Spotify and terminal",
            "close Spotify and I am tired",
            "play rock and roll music",
        ):
            self.assertEqual(split_action_clauses(phrase), [phrase])

    async def test_observed_phaxity_transcript_resolves_to_launcher_identity(self) -> None:
        normalized = normalize_transcript("VV. Closed-fact city, Nico Music.")
        self.assertEqual(extract_close_targets(normalized), ("phaxity-neko-music",))
        turn = await OfflineProvider().begin(normalized, [], [], [])
        self.assertEqual(turn.tool_calls[0].name, "system.get_processes")
        self.assertEqual(turn.tool_calls[0].arguments["query"], "phaxity-neko-music")

    async def test_spotify_phrases_always_use_mpris_and_explicit_close_stays_close(self) -> None:
        provider = OfflineProvider()
        for phrase, action in (
            ("play music off my Spotify", "play"),
            ("start playing Spotify", "play"),
            ("turn Spotify off", "pause"),
            ("stop Spotify", "pause"),
            ("skip track", "next"),
            ("go back a song", "previous"),
        ):
            with self.subTest(phrase=phrase):
                turn = await provider.begin(phrase, [], [], [])
                self.assertEqual(turn.tool_calls[0].name, "audio.media")
                expected = {"action": action}
                if "spotify" in phrase.lower():
                    expected["player"] = "spotify"
                self.assertEqual(turn.tool_calls[0].arguments, expected)
        close = await provider.begin("close Spotify", [], [], [])
        self.assertEqual(close.tool_calls[0].name, "system.get_processes")

    async def test_microphone_requests_resolve_real_device_before_switching(self) -> None:
        provider = OfflineProvider()
        status = await provider.begin("what microphone am I using?", [], [], [])
        self.assertEqual(status.tool_calls[0].name, "audio.devices")

        select = await provider.begin("use the built-in laptop mic", [], [], [])
        self.assertEqual(select.tool_calls[0].name, "audio.devices")
        devices = {
            "default_input": "bluez_input.airpods",
            "inputs": [
                {
                    "name": "alsa_input.internal",
                    "description": "Built-in Audio",
                    "device_bus": "pci",
                },
                {
                    "name": "bluez_input.airpods",
                    "description": "AirPods",
                    "device_bus": "bluetooth",
                },
            ],
        }
        resolved = await provider.continue_with_tools(
            select,
            [(select.tool_calls[0], {"status": "completed", "result": devices})],
            [],
        )
        self.assertEqual(resolved.tool_calls[0].name, "audio.default_input.set")
        self.assertEqual(resolved.tool_calls[0].arguments["source"], "alsa_input.internal")

    async def test_microphone_mute_never_falls_through_to_output_mute(self) -> None:
        provider = OfflineProvider()
        muted = await provider.begin("mute my microphone", [], [], [])
        unmuted = await provider.begin("unmute the mic", [], [], [])
        self.assertEqual(muted.tool_calls[0].name, "audio.microphone_mute.set")
        self.assertTrue(muted.tool_calls[0].arguments["muted"])
        self.assertEqual(unmuted.tool_calls[0].name, "audio.microphone_mute.set")
        self.assertFalse(unmuted.tool_calls[0].arguments["muted"])


class SpotifyToolTests(unittest.TestCase):
    def test_spotify_is_selected_exactly_and_play_is_verified(self) -> None:
        results = [
            {
                "ok": True,
                "stdout": (
                    "org.mpris.MediaPlayer2.firefox.instance_1\n"
                    "org.mpris.MediaPlayer2.spotify\n"
                    "org.mpris.MediaPlayer2.vlc\n"
                ),
                "stderr": "",
            },
            {"ok": True, "stdout": "", "stderr": ""},
            {"ok": True, "stdout": "Playing\n", "stderr": ""},
        ]
        with patch("ev.tools.builtin.run_command", side_effect=results) as run:
            result = mpris({"action": "play", "player": "spotify"}, None)
        self.assertTrue(result["ok"])
        self.assertEqual(result["player"], "org.mpris.MediaPlayer2.spotify")
        self.assertEqual(result["playback_status"], "Playing")
        self.assertEqual(run.call_args_list[1].args[0][1], "org.mpris.MediaPlayer2.spotify")

    def test_missing_spotify_does_not_control_an_unrelated_player(self) -> None:
        listing = {
            "ok": True,
            "stdout": "org.mpris.MediaPlayer2.firefox.instance_1\norg.mpris.MediaPlayer2.vlc\n",
            "stderr": "",
        }
        with (
            patch("ev.tools.builtin.run_command", return_value=listing) as run,
            patch("ev.tools.builtin.open_application", return_value={"launched": False}) as launch,
        ):
            result = mpris({"action": "play", "player": "spotify"}, None)
        self.assertFalse(result["ok"])
        self.assertIn("Spotify", result["reason"])
        self.assertEqual(run.call_count, 1)
        launch.assert_called_once_with({"desktop_id": "com.spotify.Client"}, None)

    def test_play_starts_spotify_if_it_is_not_already_open(self) -> None:
        results = [
            {"ok": True, "stdout": "org.mpris.MediaPlayer2.vlc\n", "stderr": ""},
            {"ok": True, "stdout": "org.mpris.MediaPlayer2.spotify\n", "stderr": ""},
            {"ok": True, "stdout": "", "stderr": ""},
            {"ok": True, "stdout": "Playing\n", "stderr": ""},
        ]
        with (
            patch("ev.tools.builtin.run_command", side_effect=results) as run,
            patch("ev.tools.builtin.open_application", return_value={"launched": True}) as launch,
            patch("ev.tools.builtin.time.sleep"),
        ):
            result = mpris({"action": "play", "player": "spotify"}, None)
        self.assertTrue(result["ok"])
        self.assertTrue(result["launched"])
        launch.assert_called_once_with({"desktop_id": "com.spotify.Client"}, None)


class CompoundCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_close_targets_reach_separate_submissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = CarlosCore(
                paths=Paths(
                    *(root / name for name in ("config", "data", "state", "cache", "runtime"))
                )
            )
            try:
                service.brain.submit = AsyncMock(
                    return_value={"status": "completed", "response": "Test only."}
                )
                await service._submit_action_clauses("close Spotify and my terminal window")
                self.assertEqual(
                    [entry.args[0] for entry in service.brain.submit.await_args_list],
                    ["close Spotify", "close my terminal window"],
                )
            finally:
                service.memory.close()

    async def test_typed_request_cleanly_supersedes_voice_follow_up_capture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = CarlosCore(
                paths=Paths(
                    root / "config", root / "data", root / "state", root / "cache", root / "runtime"
                )
            )
            try:
                service.voice.capture_active = True
                service.state.transition(CoreState.LISTENING, "follow-up")

                async def abort(_reason: str):
                    service.voice.capture_active = False
                    service.state.transition(CoreState.DORMANT, "new request")
                    return {"status": "cancelled"}

                service.voice.abort_capture = AsyncMock(side_effect=abort)  # type: ignore[method-assign]
                service.brain.submit = AsyncMock(
                    return_value={
                        "status": "completed",
                        "response": "ready",
                        "correlation_id": "typed",
                    }
                )  # type: ignore[method-assign]
                service._schedule_response_speech = MagicMock()  # type: ignore[method-assign]
                result = await service.handle_request(
                    {
                        "type": "command.submit",
                        "id": "typed",
                        "payload": {"text": "what is my RAM usage"},
                    }
                )
                self.assertEqual(result["status"], "completed")
                service.voice.abort_capture.assert_awaited_once_with("new_interactive_request")
                service.brain.submit.assert_awaited_once()
            finally:
                service.memory.close()

    async def test_service_runs_compound_actions_in_spoken_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = CarlosCore(
                paths=Paths(
                    root / "config", root / "data", root / "state", root / "cache", root / "runtime"
                )
            )
            try:
                # Isolate clause queuing from the real app-launch fast path.
                service.planner.try_plan = lambda *_args: None
                service.brain.submit = AsyncMock(
                    side_effect=[
                        {
                            "status": "completed",
                            "response": "I opened firefox.",
                            "duration_ms": 1.0,
                        },
                        {
                            "status": "completed",
                            "response": "I closed spotify.",
                            "duration_ms": 2.0,
                        },
                    ]
                )  # type: ignore[method-assign]
                result = await service._submit_action_clauses(
                    "Open Firefox, Ben, and close Spotify.", "compound-test"
                )
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["response"], "I opened firefox. I closed spotify.")
                self.assertEqual(
                    service.brain.submit.await_args_list,
                    [call("Open Firefox", "compound-test"), call("close Spotify", "compound-test")],
                )
            finally:
                service.memory.close()

    async def test_compound_queue_stops_before_later_actions_on_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = CarlosCore(
                paths=Paths(
                    root / "config", root / "data", root / "state", root / "cache", root / "runtime"
                )
            )
            try:
                service.planner.try_plan = lambda *_args: None
                service.brain.submit = AsyncMock(
                    side_effect=[
                        {"status": "completed", "response": "First done."},
                        {
                            "status": "confirmation_required",
                            "response": "Need permission.",
                            "confirmation": {"id": "x"},
                        },
                    ]
                )  # type: ignore[method-assign]
                result = await service._submit_action_clauses(
                    "open Firefox then close Spotify then open Discord", "compound-stop"
                )
                self.assertEqual(result["status"], "confirmation_required")
                self.assertEqual(result["response"], "First done. Need permission.")
                self.assertEqual(service.brain.submit.await_count, 2)
            finally:
                service.memory.close()


class FakeStt:
    def __init__(self, primary: str, verification: str | None, available: bool = True) -> None:
        self.primary = primary
        self.verification = verification
        self._verification_available = available
        self.transcribe_verification = AsyncMock(side_effect=self._verify)

    @property
    def available(self):
        return True, "ready"

    @property
    def verification_available(self):
        return self._verification_available, (
            "ready" if self._verification_available else "missing verifier"
        )

    async def transcribe(self, _pcm: bytes):
        return Transcript(self.primary, "primary", "primary.bin", 10.0)

    async def _verify(self, _pcm: bytes):
        assert self.verification is not None
        return Transcript(self.verification, "verifier", "verifier.bin", 20.0)


class VoiceCloseConsensusTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _armed_manager(primary: str, verification: str | None, available: bool = True):
        bus = PhaxEventBus()
        state = StateMachine(bus)
        manager = VoiceManager(
            {"capture_command": ["/missing"], "wake": {"enabled": False}}, bus, state
        )
        manager.stt = FakeStt(primary, verification, available)  # type: ignore[assignment]
        pcm = int(5000).to_bytes(2, "little", signed=True) * 16000
        manager.capture_active = True
        manager.capture_origin = "ambient"
        manager.capture_started = time.monotonic() - 1
        manager.capture_bytes = len(pcm)
        manager.capture_pcm.extend(pcm)
        manager.capture_speech_start_byte = 0
        manager.capture_speech_end_byte = len(pcm)
        manager.capture_mode = "wake_command"
        manager.diagnostics.update({"max_rms": 0.1, "max_peak": 0.2})
        state.transition(CoreState.LISTENING, "test")
        manager.command_handler = AsyncMock(
            return_value={
                "status": "completed",
                "response": "done",
                "correlation_id": "voice-close",
                "cognition": {},
            }
        )
        manager.response_handler = MagicMock()
        return manager, state, bus

    async def test_different_recognized_app_names_block_every_tool(self) -> None:
        manager, state, bus = self._armed_manager("Close Firefox.", "Close Spotify.")
        result = await manager.stop_capture("voice-close")
        manager.command_handler.assert_not_awaited()  # type: ignore[union-attr]
        self.assertTrue(result["command"]["safety_blocked"])
        self.assertEqual(result["close_verification"]["primary_targets"], ["firefox"])
        self.assertEqual(result["close_verification"]["verification_targets"], ["spotify"])
        self.assertEqual(state.current, CoreState.DORMANT)
        self.assertTrue(
            any(event["type"] == "voice.close_verification_complete" for event in bus.history())
        )

    async def test_matching_app_names_reach_the_normal_command_pipeline(self) -> None:
        manager, _state, _bus = self._armed_manager("Close Spotify.", "Close Spotify.")
        result = await manager.stop_capture("voice-close")
        manager.command_handler.assert_awaited_once_with("Close Spotify.", "voice-close")  # type: ignore[union-attr]
        self.assertEqual(result["close_targets_verified"], ["spotify"])
        self.assertEqual(result["command"]["status"], "completed")

    async def test_missing_verifier_blocks_instead_of_falling_back_to_trust(self) -> None:
        manager, _state, _bus = self._armed_manager("Close Spotify.", None, available=False)
        result = await manager.stop_capture("voice-close")
        manager.command_handler.assert_not_awaited()  # type: ignore[union-attr]
        self.assertTrue(result["command"]["safety_blocked"])
        self.assertEqual(result["close_verification"]["verification_targets"], [])


if __name__ == "__main__":
    unittest.main()
