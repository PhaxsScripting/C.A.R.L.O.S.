from __future__ import annotations

import asyncio
import json
import math
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "core"))

from ev.events import PhaxEventBus  # noqa: E402
from ev.ai import (
    LocalHybridProvider,
    LocalLlamaProvider,
    OfflineProvider,
    Provider,
    ProviderTurn,
    ToolCall,
)  # noqa: E402
from ev.ai.openai_responses import strict_schema  # noqa: E402
from ev.ipc.protocol import ProtocolError, decode_message, encode_message  # noqa: E402
from ev.memory import MemoryStore  # noqa: E402
from ev.paths import Paths  # noqa: E402
from ev.permissions import Permission, PermissionBroker  # noqa: E402
from ev.planner import TaskPlan  # noqa: E402
from ev.service import CarlosCore  # noqa: E402
from ev.state import CoreState, StateMachine  # noqa: E402
from ev.telemetry import TelemetrySampler  # noqa: E402
from ev.tools import (
    ToolContext,
    ToolRegistry,
    ValidationError,
    register_builtin_tools,
)  # noqa: E402
from ev.tools.builtin import get_processes  # noqa: E402
from ev.voice import (
    EnergyVad,
    PcmHighPass,
    VoiceManager,
    is_conversation_stop,
    is_negative_action,
    normalize_transcript,
)  # noqa: E402
from ev.voice.tts import PiperAdapter, SynthesizedAudio, TtsRouter  # noqa: E402


class EventTests(unittest.TestCase):
    def test_real_event_is_sequenced_and_redacted(self) -> None:
        bus = PhaxEventBus(history_limit=4)
        event = bus.publish(
            "tool.started", "tools", {"tool": "system.memory", "token": "secret"}, "corr"
        )
        self.assertEqual(event.sequence, 1)
        self.assertEqual(event.as_dict()["payload"]["token"], "[REDACTED]")
        self.assertEqual(bus.history()[0]["correlation_id"], "corr")

    def test_subscriber_queue_receives_event(self) -> None:
        bus = PhaxEventBus()
        subscriber, queue = bus.subscribe()
        expected = bus.publish("system.telemetry", "telemetry", {"cpu": 1})
        self.assertIs(queue.get_nowait(), expected)
        bus.unsubscribe(subscriber)

    def test_slow_subscriber_keeps_newest_bounded_events(self) -> None:
        bus = PhaxEventBus(queue_size=2)
        _subscriber, queue = bus.subscribe()
        for index in range(5):
            bus.publish("test.event", "test", {"index": index})
        self.assertEqual(queue.qsize(), 2)
        self.assertEqual(queue.get_nowait().payload["index"], 3)
        self.assertEqual(queue.get_nowait().payload["index"], 4)

    def test_audio_samples_stay_live_without_evicting_task_history(self) -> None:
        bus = PhaxEventBus(history_limit=4, queue_size=16)
        _subscriber, queue = bus.subscribe()
        important = bus.publish("tool.completed", "tools", {"tool": "system.get_memory_usage"})
        for index in range(20):
            bus.publish("voice.audio_level", "voice", {"index": index})
        self.assertEqual([item["type"] for item in bus.history()], ["tool.completed"])
        self.assertTrue(any(item.type == "voice.audio_level" for item in list(queue._queue)))
        self.assertEqual(bus.sequence, important.sequence + 20)


class StateTests(unittest.TestCase):
    def test_valid_transition_emits_event(self) -> None:
        bus = PhaxEventBus()
        state = StateMachine(bus)
        state.transition(CoreState.THINKING, "test")
        self.assertEqual(state.current, CoreState.THINKING)
        self.assertEqual(bus.history()[0]["type"], "core.state_changed")

    def test_invalid_transition_is_rejected(self) -> None:
        state = StateMachine(PhaxEventBus())
        with self.assertRaises(ValueError):
            state.transition(CoreState.TRANSCRIBING, "invalid from dormant")


class ProtocolTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        line = encode_message({"type": "health", "id": "1", "payload": {}})
        self.assertEqual(decode_message(line, 1024)["type"], "health")

    def test_invalid_message_rejected(self) -> None:
        with self.assertRaises(ProtocolError):
            decode_message(b"[]", 1024)

    def test_versioned_and_legacy_clients_share_v1_but_unknown_versions_are_rejected(self):
        for request in ({"type": "health"}, {"type": "health", "protocol_version": 1}):
            self.assertEqual(decode_message(json.dumps(request).encode(), 1024)["type"], "health")
        for version in (2, "1", True):
            with self.assertRaisesRegex(ProtocolError, "unsupported_protocol_version"):
                decode_message(
                    json.dumps({"type": "health", "protocol_version": version}).encode(), 1024
                )


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_spaced_computer_acronyms_are_normalized_for_commands(self) -> None:
        self.assertEqual(normalize_transcript("What is my R A M usage?"), "What is my RAM usage?")
        self.assertEqual(
            normalize_transcript("C.P.U. and G-P-U temperature"), "CPU and GPU temperature"
        )

    async def test_process_intent_precedes_generic_memory_intent(self) -> None:
        turn = await OfflineProvider().begin("what's using all my RAM", [], [], [])
        self.assertEqual(turn.tool_calls[0].name, "system.get_processes")
        self.assertEqual(turn.tool_calls[0].arguments["sort"], "memory")

    async def test_spoken_wake_alias_and_short_ram_question_use_local_tool(self) -> None:
        turn = await OfflineProvider().begin("Eevee. What's my RAM?", [], [], [])
        self.assertEqual(turn.tool_calls[0].name, "system.get_memory_usage")

    async def test_natural_new_window_phrase_resolves_application_name(self) -> None:
        turn = await OfflineProvider().begin("Eevee. Open another thing of Firefox.", [], [], [])
        self.assertEqual(turn.tool_calls[0].name, "applications.list")
        self.assertEqual(turn.tool_calls[0].arguments["query"], "firefox")

    async def test_compound_browser_request_does_not_pollute_application_name(self) -> None:
        turn = await OfflineProvider().begin(
            "Open my Firefox browser, then open a new tab and look up how to cook beans.",
            [],
            [],
            [],
        )
        self.assertEqual(turn.tool_calls[0].name, "applications.list")
        self.assertEqual(turn.tool_calls[0].arguments["query"], "firefox")

    async def test_find_application_is_not_misrouted_to_file_scan(self) -> None:
        for request in ("find Firefox", "And find Firefox.", "Why can't you find Firefox?"):
            with self.subTest(request=request):
                turn = await OfflineProvider().begin(request, [], [], [])
                self.assertEqual(turn.tool_calls[0].name, "applications.list")
                self.assertEqual(turn.tool_calls[0].arguments["query"], "firefox")
                self.assertTrue(turn.continuation["lookup_only"])

    async def test_explicit_remember_intent_uses_memory_tool(self) -> None:
        turn = await OfflineProvider().begin(
            "E.V., remember that my demo is in Downloads", [], [], []
        )
        self.assertEqual(turn.tool_calls[0].name, "memory.remember")
        self.assertEqual(turn.tool_calls[0].arguments["content"], "my demo is in Downloads")

    async def test_unambiguous_application_open_is_a_second_structured_tool(self) -> None:
        provider = OfflineProvider()
        turn = await provider.begin("open Firefox", [], [], [])
        self.assertEqual(turn.tool_calls[0].name, "applications.list")
        continued = await provider.continue_with_tools(
            turn,
            [
                (
                    turn.tool_calls[0],
                    {
                        "status": "completed",
                        "result": {"applications": [{"name": "Firefox", "desktop_id": "firefox"}]},
                    },
                )
            ],
            [],
        )
        self.assertEqual(continued.tool_calls[0].name, "applications.open")
        self.assertEqual(continued.tool_calls[0].arguments["desktop_id"], "firefox")

    async def test_highest_unique_application_match_wins_without_false_ambiguity(self) -> None:
        provider = OfflineProvider()
        turn = await provider.begin("open Firefox", [], [], [])
        applications = [
            {"name": "Mozilla Firefox", "desktop_id": "firefox-bin", "match_score": 100},
            {"name": "Firefox Profile Manager", "desktop_id": "firefox-profile", "match_score": 80},
        ]
        continued = await provider.continue_with_tools(
            turn,
            [
                (
                    turn.tool_calls[0],
                    {"status": "completed", "result": {"applications": applications}},
                )
            ],
            [],
        )
        self.assertEqual(continued.tool_calls[0].name, "applications.open")
        self.assertEqual(continued.tool_calls[0].arguments["desktop_id"], "firefox-bin")

    async def test_natural_close_alias_resolves_to_confirmed_process_action(self) -> None:
        provider = OfflineProvider()
        turn = await provider.begin("close vscode", [], [], [])
        self.assertEqual(turn.tool_calls[0].name, "system.get_processes")
        self.assertEqual(turn.tool_calls[0].arguments["query"], "code")
        continued = await provider.continue_with_tools(
            turn,
            [
                (
                    turn.tool_calls[0],
                    {
                        "status": "completed",
                        "result": {
                            "processes": [
                                {"pid": 123, "ppid": 1, "name": "code", "started_at_epoch": 42.0}
                            ]
                        },
                    },
                )
            ],
            [],
        )
        self.assertEqual(continued.tool_calls[0].name, "applications.close_process")
        self.assertEqual(
            continued.tool_calls[0].arguments,
            {
                "pid": 123,
                "started_at_epoch": 42.0,
                "expected_query": "code",
            },
        )

    async def test_spotify_main_process_is_distinguished_from_crash_helper(self) -> None:
        provider = OfflineProvider()
        turn = await provider.begin("close Spotify", [], [], [])
        rows = [
            {"pid": 100, "ppid": 50, "name": "spotify", "started_at_epoch": 10.0},
            {"pid": 101, "ppid": 100, "name": "spotify", "started_at_epoch": 11.0},
            {"pid": 102, "ppid": 50, "name": "spotify", "started_at_epoch": 12.0},
        ]
        continued = await provider.continue_with_tools(
            turn,
            [(turn.tool_calls[0], {"status": "completed", "result": {"processes": rows}})],
            [],
        )
        self.assertEqual(continued.tool_calls[0].name, "applications.close_process")
        self.assertEqual(
            continued.tool_calls[0].arguments,
            {
                "pid": 100,
                "started_at_epoch": 10.0,
                "expected_query": "spotify",
            },
        )

    async def test_common_close_verbs_share_confirmation_routing(self) -> None:
        for phrase, query in (("stop Discord", "discord"), ("shut down Spotify", "spotify")):
            with self.subTest(phrase=phrase):
                turn = await OfflineProvider().begin(phrase, [], [], [])
                self.assertEqual(turn.tool_calls[0].name, "system.get_processes")
                self.assertEqual(turn.tool_calls[0].arguments["query"], query)
        negated = await OfflineProvider().begin("don't shut down Spotify", [], [], [])
        self.assertEqual(negated.tool_calls, [])

    async def test_negated_close_never_selects_a_tool(self) -> None:
        turn = await OfflineProvider().begin("don't close VS Code", [], [], [])
        self.assertEqual(turn.tool_calls, [])
        self.assertIn("won’t", turn.text)

    async def test_contextual_follow_up_reuses_recent_process_without_bypassing_tools(self) -> None:
        provider = OfflineProvider()
        context = [
            {"role": "user", "content": "what's using the most RAM?"},
            {
                "role": "assistant",
                "content": "Top memory users are firefox at 2100 MB, code at 500 MB.",
            },
        ]
        amount = await provider.begin("How much?", context, [], [])
        self.assertEqual(amount.tool_calls, [])
        self.assertIn("firefox", amount.text)
        close = await provider.begin("Close it.", context, [], [])
        self.assertEqual(close.tool_calls[0].name, "system.get_processes")
        self.assertEqual(close.tool_calls[0].arguments["query"], "firefox")

    async def test_usage_followups_sample_live_processes_without_model_reasoning(self) -> None:
        for subject in ("RAM", "CPU"):
            context = [{"role": "user", "content": f"What's my {subject} usage?"}]
            for phrase in (
                "what's using the most?",
                "Which application is using the most right now?",
                "What process is using the most right now?",
                "process is using the most right now.",
            ):
                with self.subTest(subject=subject, phrase=phrase):
                    turn = await OfflineProvider().begin(phrase, context, [], [])
                    self.assertEqual(turn.tool_calls[0].name, "system.get_processes")
                    self.assertEqual(
                        turn.tool_calls[0].arguments["sort"],
                        "memory" if subject == "RAM" else "cpu",
                    )
        for phrase in (
            "Explain how a process is using the most RAM",
            "don't check which process is using the most",
        ):
            turn = await OfflineProvider().begin(phrase, context, [], [])
            self.assertFalse(turn.tool_calls)

    async def test_process_identity_followup_is_lookup_only(self) -> None:
        context = [
            {
                "role": "assistant",
                "content": "Top memory users are firefox at 2100 MB, code at 500 MB.",
            }
        ]
        turn = await OfflineProvider().begin(
            "what application does that process connect to?", context, [], []
        )
        self.assertEqual(turn.tool_calls[0].name, "applications.list")
        self.assertEqual(turn.tool_calls[0].arguments["query"], "firefox")
        self.assertTrue(turn.continuation["lookup_only"])

    def test_openai_schema_is_strict_and_optional_is_nullable(self) -> None:
        schema = strict_schema(
            {
                "type": "object",
                "properties": {"required": {"type": "string"}, "optional": {"type": "integer"}},
                "required": ["required"],
            }
        )
        self.assertEqual(set(schema["required"]), {"required", "optional"})
        self.assertEqual(schema["properties"]["optional"]["type"], ["integer", "null"])
        self.assertFalse(schema["additionalProperties"])

    async def test_non_coding_sensitive_tool_runs_without_confirmation(self) -> None:
        class FakeProvider(Provider):
            name = "fake"

            @property
            def available(self):
                return True, "test"

            @property
            def model(self):
                return "test-model"

            async def begin(self, user_text, context, memories, tools):
                return ProviderTurn(
                    self.name,
                    self.model,
                    "",
                    [ToolCall("call-1", "files.read", {"path": str(PROJECT / "README.md")})],
                )

            async def continue_with_tools(self, turn, outputs, tools):
                return ProviderTurn(
                    self.name, self.model, f"tool status: {outputs[-1][1]['status']}"
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = CarlosCore(
                paths=Paths(
                    root / "config", root / "data", root / "state", root / "cache", root / "runtime"
                )
            )
            try:
                service.tools.context.config["security"]["allowed_roots"] = [str(PROJECT)]
                service.brain.provider = FakeProvider()
                completed = await service.brain.submit("read the project readme")
                self.assertEqual(completed["status"], "completed")
                self.assertEqual(completed["response"], "tool status: completed")
            finally:
                service.memory.close()


class VoiceTests(unittest.TestCase):
    @staticmethod
    def _sine_pcm(frequency: float, seconds: float = 1.0, amplitude: float = 0.5) -> bytes:
        sample_count = int(16000 * seconds)
        return b"".join(
            int(32767 * amplitude * math.sin(2 * math.pi * frequency * index / 16000)).to_bytes(
                2,
                "little",
                signed=True,
            )
            for index in range(sample_count)
        )

    def test_highpass_removes_rumble_and_preserves_speech_band(self) -> None:
        low_filter = PcmHighPass(140)
        high_filter = PcmHighPass(140)
        low = low_filter.process(self._sine_pcm(20.0))
        high = high_filter.process(self._sine_pcm(1000.0))
        low_rms = VoiceManager._levels(low[8000:])["rms"]
        high_rms = VoiceManager._levels(high[8000:])["rms"]
        self.assertLess(low_rms, 0.015)
        self.assertGreater(high_rms, 0.30)

    def test_seeded_vad_treats_steady_background_as_noise(self) -> None:
        vad = EnergyVad({"minimum_rms": 0.01, "noise_ratio": 2.5, "start_ms": 100})
        vad.reset(0.04)
        background = [vad.update(0.045, 0.10, 50) for _ in range(8)]
        self.assertFalse(any(update.speech_started for update in background))
        speech = [vad.update(0.15, 0.40, 50) for _ in range(3)]
        self.assertTrue(any(update.speech_started for update in speech))

    def test_waveform_uses_real_pcm_samples(self) -> None:
        samples = (0, 16384, -32768, 8192)
        pcm = b"".join(int(sample).to_bytes(2, "little", signed=True) for sample in samples)
        levels = VoiceManager._levels(pcm, points=4)
        self.assertEqual(levels["waveform"], [0.0, 0.5, -1.0, 0.25])
        self.assertEqual(levels["peak"], 1.0)

    def test_levels_expose_clipping_and_dc_offset(self) -> None:
        pcm = b"".join(
            int(sample).to_bytes(2, "little", signed=True)
            for sample in (32767, 32767, 16384, 16384)
        )
        levels = VoiceManager._levels(pcm, points=4)
        self.assertEqual(levels["clip_ratio"], 0.5)
        self.assertGreater(levels["dc_offset"], 0.7)

    def test_sustained_invalid_input_stops_capture_before_stt(self) -> None:
        manager = VoiceManager(
            {
                "capture_command": ["/definitely/missing"],
                "wake": {"enabled": False, "invalid_input_hold_ms": 1000, "gate_maximum_rms": 0.18},
            },
            PhaxEventBus(),
            StateMachine(PhaxEventBus()),
        )
        clipped_frame = int(32767).to_bytes(2, "little", signed=True) * 800
        for _ in range(19):
            self.assertFalse(manager._consume_capture_chunk(clipped_frame))
        self.assertTrue(manager._consume_capture_chunk(clipped_frame))
        self.assertEqual(manager.capture_auto_reason, "invalid_input")
        self.assertEqual(manager._capture_health(""), "INPUT_INVALID")

    def test_normal_laptop_microphone_level_is_not_rejected(self) -> None:
        manager = VoiceManager(
            {
                "capture_command": ["/definitely/missing"],
                "wake": {
                    "enabled": False,
                    "invalid_input_hold_ms": 1000,
                    "gate_maximum_rms": 0.75,
                    "gate_maximum_dc_offset": 0.5,
                },
            },
            PhaxEventBus(),
            StateMachine(PhaxEventBus()),
        )
        frame = b"".join(
            int(sample).to_bytes(2, "little", signed=True)
            for sample in ([12000, 9000, -10000, -7000] * 200)
        )
        for _ in range(25):
            self.assertFalse(manager._consume_capture_chunk(frame))
        self.assertNotEqual(manager._capture_health(""), "INPUT_INVALID")

    def test_unavailable_capture_is_reported_without_fake_audio(self) -> None:
        manager = VoiceManager(
            {"capture_command": ["/definitely/missing"]},
            PhaxEventBus(),
            StateMachine(PhaxEventBus()),
        )
        self.assertFalse(manager.snapshot()["capture_available"])
        self.assertEqual(manager.snapshot()["last_input_level"]["waveform"], [])

    def test_normalization_removes_fillers_but_preserves_negation(self) -> None:
        self.assertEqual(normalize_transcript("E.V., close uhhhh vscode"), "close vscode")
        self.assertEqual(normalize_transcript("Eevee. What's my RAM?"), "What's my RAM?")
        self.assertEqual(
            normalize_transcript("Well, um, what's my RAM usage?"), "what's my RAM usage?"
        )
        self.assertEqual(normalize_transcript("I like Firefox"), "I like Firefox")
        negated = normalize_transcript("E.V., don't uhhh close VS Code")
        self.assertEqual(negated, "don't close VS Code")
        self.assertTrue(is_negative_action(negated))
        self.assertTrue(is_conversation_stop("E.V., stop talking"))
        self.assertTrue(is_conversation_stop("shut up"))
        self.assertTrue(is_conversation_stop("shut the fuck up"))
        self.assertTrue(is_conversation_stop("I'm done"))
        self.assertTrue(is_conversation_stop("end the conversation"))
        self.assertTrue(is_conversation_stop("no thanks"))
        self.assertTrue(is_conversation_stop("that's all"))
        self.assertFalse(is_conversation_stop("stop Firefox"))

    def test_adaptive_vad_waits_through_short_pause_and_ends_on_long_silence(self) -> None:
        vad = EnergyVad(
            {"minimum_rms": 0.01, "start_ms": 100, "minimum_speech_ms": 250, "end_silence_ms": 500}
        )
        for _ in range(5):
            update = vad.update(0.002, 0.004, 50)
        self.assertFalse(update.active)
        starts = [vad.update(0.05, 0.15, 50) for _ in range(5)]
        self.assertTrue(any(update.speech_started for update in starts))
        short_pause = [vad.update(0.002, 0.004, 50) for _ in range(4)]
        self.assertFalse(any(update.speech_ended for update in short_pause))
        vad.update(0.05, 0.15, 50)
        ending = [vad.update(0.002, 0.004, 50) for _ in range(10)]
        self.assertTrue(ending[-1].speech_ended)

    def test_vad_reports_timing_needed_for_safe_stt_boundaries(self) -> None:
        vad = EnergyVad(
            {"minimum_rms": 0.01, "start_ms": 100, "minimum_speech_ms": 200, "end_silence_ms": 300}
        )
        for _ in range(20):
            vad.update(0.001, 0.003, 50)
        starts = [vad.update(0.05, 0.12, 50) for _ in range(4)]
        self.assertEqual(next(item for item in starts if item.speech_started).speech_ms, 100)
        ending = [vad.update(0.001, 0.003, 50) for _ in range(6)]
        self.assertTrue(ending[-1].speech_ended)
        self.assertEqual(ending[-1].silence_ms, 300)

    def test_default_vad_uses_responsive_endpoint_without_cutting_short_pause(self) -> None:
        vad = EnergyVad({"end_silence_ms": 1600, "responsive_end_silence_ms": 1000})
        for _ in range(6):
            vad.update(0.05, 0.12, 50)
        short_pause = [vad.update(0.001, 0.003, 50) for _ in range(12)]
        self.assertFalse(any(item.speech_ended for item in short_pause))
        ending = [vad.update(0.001, 0.003, 50) for _ in range(8)]
        self.assertTrue(ending[-1].speech_ended)
        self.assertEqual(ending[-1].silence_ms, 1000)


class VoiceStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_phrase_cancels_speech_while_synthesis_is_loading(self) -> None:
        bus = PhaxEventBus()
        manager = VoiceManager(
            {
                "capture_command": ["/missing"],
                "wake": {"enabled": False},
                "follow_up": {"enabled": True},
            },
            bus,
            StateMachine(bus),
        )
        synthesis_started = asyncio.Event()
        release_synthesis = asyncio.Event()

        async def delayed_synthesis(_text: str) -> SynthesizedAudio:
            synthesis_started.set()
            await release_synthesis.wait()
            return SynthesizedAudio(b"\0\0", 22050, 1, 2, "test", "test", 1.0)

        manager.tts.synthesize = delayed_synthesis  # type: ignore[method-assign]
        with patch.object(
            VoiceManager, "tts_available", new_callable=PropertyMock, return_value=True
        ):
            speech_task = asyncio.create_task(
                manager.speak("success", "corr", allow_follow_up=True)
            )
            await synthesis_started.wait()
            stopped = await manager.stop_speaking("spoken_stop", "stop-corr")
            release_synthesis.set()
            result = await speech_task

        self.assertEqual(stopped["status"], "cancelled")
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse(manager.capture_active)
        self.assertEqual(manager.diagnostics["follow_up_state"], "IDLE")

    async def test_hot_system_keeps_selected_neural_voice(self) -> None:
        router = TtsRouter({"provider": "piper", "thermal_pause_celsius": 97.0})
        audio = SynthesizedAudio(b"\0\0", 22050, 1, 2, "piper", "E.V. American", 1.0)
        router.piper.synthesize = AsyncMock(return_value=audio)  # type: ignore[method-assign]
        router.espeak.synthesize = AsyncMock()  # type: ignore[method-assign]
        with (
            patch.object(
                PiperAdapter, "available", new_callable=PropertyMock, return_value=(True, "ready")
            ),
            patch(
                "ev.voice.tts.read_temperature", return_value={"celsius": 95.0, "sensor": "test"}
            ),
        ):
            result = await router.synthesize("hello")
        self.assertEqual(result.engine, "piper")
        router.piper.synthesize.assert_awaited_once_with("hello")
        router.espeak.synthesize.assert_not_awaited()

    async def test_critical_temperature_pauses_instead_of_changing_voice(self) -> None:
        router = TtsRouter({"provider": "piper", "thermal_pause_celsius": 97.0})
        router.piper.synthesize = AsyncMock()  # type: ignore[method-assign]
        router.espeak.synthesize = AsyncMock()  # type: ignore[method-assign]
        with patch("ev.voice.tts.read_temperature", return_value={"celsius": 98.0}):
            with self.assertRaisesRegex(RuntimeError, "reply is available in chat"):
                await router.synthesize("hello")
        router.piper.synthesize.assert_not_awaited()
        router.espeak.synthesize.assert_not_awaited()

    def test_missing_neural_voice_is_reported_without_silent_substitution(self) -> None:
        router = TtsRouter({"provider": "piper", "python": "/missing/piper"})
        self.assertIs(router.adapter, router.piper)
        self.assertFalse(router.available[0])

    async def test_neural_recovery_uses_bounded_worker_and_cleans_up(self) -> None:
        adapter = PiperAdapter({})
        with (
            patch.object(
                PiperAdapter, "_synthesize_persistent", new_callable=AsyncMock
            ) as synthesize,
            patch.object(PiperAdapter, "close", new_callable=AsyncMock) as close,
        ):
            await adapter._synthesize_oneshot("hello")
            synthesize.assert_awaited_once_with("hello")
            close.assert_awaited_once()
        with (
            patch.object(
                PiperAdapter,
                "_synthesize_persistent",
                new_callable=AsyncMock,
                side_effect=RuntimeError("failed"),
            ),
            patch.object(PiperAdapter, "close", new_callable=AsyncMock) as close,
        ):
            with self.assertRaisesRegex(RuntimeError, "failed"):
                await adapter._synthesize_oneshot("hello")
            close.assert_awaited_once()

    async def test_wake_gate_batches_complete_pcm_without_dropping_audio(self) -> None:
        class FakeStdout:
            def __init__(self, chunks: list[bytes]) -> None:
                self.chunks = chunks

            async def read(self, _size: int) -> bytes:
                return self.chunks.pop(0) if self.chunks else b""

        class FakeProcess:
            def __init__(self, chunks: list[bytes]) -> None:
                self.stdout = FakeStdout(chunks)

        bus = PhaxEventBus()
        manager = VoiceManager(
            {
                "capture_command": ["/missing"],
                "wake": {
                    "enabled": False,
                    "gate_analysis_ms": 100,
                    "gate_minimum_rms": 0.008,
                    "gate_peak": 0.03,
                },
            },
            bus,
            StateMachine(bus),
        )
        frame = int(2000).to_bytes(2, "little", signed=True) * 800
        manager.wake.feed = AsyncMock()  # type: ignore[method-assign]
        await manager._wake_audio_loop(FakeProcess([frame, frame, frame, frame]))  # type: ignore[arg-type]
        self.assertEqual(manager.wake.feed.await_count, 2)
        self.assertTrue(
            all(len(call.args[0]) == 3200 for call in manager.wake.feed.await_args_list)
        )

    async def test_privacy_mode_releases_capture_and_marks_private(self) -> None:
        bus = PhaxEventBus()
        state = StateMachine(bus)
        manager = VoiceManager(
            {"capture_command": ["/missing"], "wake": {"enabled": False}}, bus, state
        )
        manager.capture_active = True
        manager.capture_origin = "ambient"
        manager.capture_pcm.extend(b"private audio")
        result = await manager.set_privacy_mode(True)
        self.assertTrue(result["privacy_mode"])
        self.assertFalse(manager.capture_active)
        self.assertEqual(manager.capture_pcm, bytearray())
        self.assertEqual(manager.diagnostics["wake_state"], "PRIVATE")

    async def test_real_wake_event_hands_rolling_audio_to_capture(self) -> None:
        bus = PhaxEventBus()
        state = StateMachine(bus)
        manager = VoiceManager(
            {"capture_command": ["/missing"], "wake": {"enabled": True}}, bus, state
        )
        manager.wake_last_feed_at = time.monotonic()
        manager.wake_buffer.append(b"rolling-pcm")
        manager._activate_capture = AsyncMock()  # type: ignore[method-assign]
        await manager._on_wake_detected({"keyword": "E_V"})
        manager._activate_capture.assert_awaited_once()
        arguments = manager._activate_capture.await_args.args
        self.assertEqual(arguments[1:3], ("wake_command", "ambient"))
        self.assertEqual(arguments[-1], b"rolling-pcm")
        self.assertTrue(any(event["type"] == "wake.detected" for event in bus.history()))

    async def test_critical_resource_mode_suspends_wake_without_changing_user_setting(self) -> None:
        manager = VoiceManager(
            {"capture_command": ["/missing"], "wake": {"enabled": True}},
            PhaxEventBus(),
            StateMachine(PhaxEventBus()),
        )
        await manager.set_resource_mode("CRITICAL")
        self.assertTrue(manager.wake_desired)
        self.assertTrue(manager.resource_suspended)
        self.assertEqual(manager.diagnostics["wake_state"], "RESOURCE_SUSPENDED")
        await manager.set_resource_mode("NORMAL")
        self.assertFalse(manager.resource_suspended)
        if manager.wake_supervisor_task is not None:
            manager.wake_desired = False
            manager.wake_supervisor_task.cancel()
            await asyncio.gather(manager.wake_supervisor_task, return_exceptions=True)


class TelemetryTests(unittest.IsolatedAsyncioTestCase):
    @patch("ev.telemetry.read_temperature", return_value={"celsius": 95.0, "sensor": "fixture"})
    async def test_threshold_emits_real_warning_event(self, temperature_reader) -> None:
        bus = PhaxEventBus()
        sampler = TelemetrySampler(
            bus, 1.0, {"warning_temperature_celsius": 90, "warning_repeat_seconds": 0}
        )
        stop = asyncio.Event()
        task = asyncio.create_task(sampler.run(stop))
        for _ in range(50):
            if any(event["type"] == "system.warning" for event in bus.history()):
                break
            await asyncio.sleep(0.01)
        stop.set()
        await task
        self.assertTrue(any(event["type"] == "system.warning" for event in bus.history()))


class MemoryTests(unittest.TestCase):
    def test_explicit_memory_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / "memory.db")
            memory = store.remember("My test project is here", ["Project", "project"])
            self.assertEqual(memory["tags"], ["project"])
            self.assertEqual(store.list_memories("test")[0]["id"], memory["id"])
            self.assertTrue(store.forget(memory["id"]))
            self.assertEqual(store.list_memories(), [])
            store.close()


class PermissionTests(unittest.TestCase):
    def test_confirmation_is_exact_and_single_use(self) -> None:
        broker = PermissionBroker(90)
        pending = broker.create(
            "files.read", {"path": "/tmp/example"}, Permission.SENSITIVE, "private", "corr"
        )
        with self.assertRaises(ValueError):
            broker.resolve(pending.id, "incorrect", True)
        resolved, approved = broker.resolve(pending.id, pending.token, True)
        self.assertTrue(approved)
        self.assertEqual(
            resolved.arguments_hash, broker.arguments_hash("files.read", {"path": "/tmp/example"})
        )
        with self.assertRaises(ValueError):
            broker.resolve(pending.id, pending.token, True)

    def test_cancelled_confirmation_cannot_be_used_later(self) -> None:
        broker = PermissionBroker(90)
        pending = broker.create(
            "desktop.window.close", {"window_id": "test"}, Permission.SENSITIVE, "close", "corr"
        )
        self.assertIs(broker.cancel(pending.id), pending)
        with self.assertRaises(ValueError):
            broker.resolve(pending.id, pending.token, True)


class ToolRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        import logging
        from copy import deepcopy
        from ev.config import DEFAULT_CONFIG

        self.config = deepcopy(DEFAULT_CONFIG)
        self.bus = PhaxEventBus()
        self.registry = ToolRegistry(ToolContext(self.config, self.bus, logging.getLogger("test")))
        register_builtin_tools(self.registry)

    def test_catalog_has_structured_permission_metadata(self) -> None:
        catalog = self.registry.catalog()
        self.assertGreaterEqual(len(catalog), 25)
        by_name = {tool["name"]: tool for tool in catalog}
        self.assertEqual(by_name["system.get_memory_usage"]["permission"], "SAFE")
        self.assertEqual(by_name["files.read"]["permission"], "SENSITIVE")
        self.assertEqual(by_name["memory.remember"]["permission"], "SENSITIVE")
        self.assertEqual(by_name["memory.forget"]["permission"], "DESTRUCTIVE")
        self.assertTrue(by_name["memory.forget"]["requires_confirmation"])
        self.assertTrue(by_name["memory.forget"]["confirmation_reason"])
        self.assertTrue(by_name["development.coding_agent_execute"]["requires_confirmation"])
        self.assertNotIn("shell", by_name)

    def test_unknown_arguments_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.registry.validate("system.get_memory_usage", {"unexpected": True})

    def test_schema_patterns_are_enforced(self) -> None:
        with self.assertRaises(ValidationError):
            self.registry.validate("development.coding_agent_result", {"proposal_id": "not-an-id"})

    def test_allowed_root_enforced(self) -> None:
        spec, _arguments = self.registry.validate("files.info", {"path": "/etc/passwd"})
        with self.assertRaises(ValidationError):
            spec.executor({"path": "/etc/passwd"}, self.registry.context)

    def test_application_scan_includes_xdg_data_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            applications = Path(directory) / "applications"
            applications.mkdir()
            (applications / "com.example.FlatApp.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=Flat Example\nExec=/bin/true\n",
                encoding="utf-8",
            )
            (applications / "com.example.FlatHelper.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=Flat Example Helper\nExec=/bin/true\n",
                encoding="utf-8",
            )
            (applications / "com.example.Hidden.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=Flat Example\nHidden=true\nExec=/bin/true\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"XDG_DATA_DIRS": directory}):
                spec, arguments = self.registry.validate(
                    "applications.list",
                    {"query": "Flat Example", "limit": 5},
                )
                result = asyncio.run(spec.executor(arguments, self.registry.context))
            self.assertEqual(result["applications"][0]["desktop_id"], "com.example.FlatApp")
            self.assertEqual(result["applications"][0]["match_score"], 100)
            self.assertNotIn(
                "com.example.Hidden", {item["desktop_id"] for item in result["applications"]}
            )

    def test_application_scan_matches_executable_identity_and_reports_running(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            applications = Path(directory) / "applications"
            applications.mkdir()
            (applications / "com.example.Audio.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=Audio Center\n"
                "Exec=/home/test/bin/phaxity-neko-music\nStartupWMClass=NekoAudio\n",
                encoding="utf-8",
            )
            with (
                patch.dict(os.environ, {"XDG_DATA_DIRS": directory}),
                patch(
                    "ev.tools.builtin._running_process_keys",
                    return_value={"phaxity neko music": {77}},
                ),
            ):
                spec, arguments = self.registry.validate(
                    "applications.list",
                    {"query": "phaxity neko music", "limit": 5},
                )
                result = asyncio.run(spec.executor(arguments, self.registry.context))
            match = result["applications"][0]
            self.assertEqual(match["desktop_id"], "com.example.Audio")
            self.assertEqual(match["match_score"], 100)
            self.assertTrue(match["running"])
            self.assertEqual(match["pids"], [77])

    def test_process_query_matches_executable_tokens_without_exposing_command_line(self) -> None:
        class FakeProcess:
            def __init__(self) -> None:
                self.info = {
                    "pid": 77,
                    "name": "chrome",
                    "username": "phax",
                    "memory_info": SimpleNamespace(rss=1234),
                    "create_time": 42.0,
                    "exe": "/opt/google/chrome/google-chrome-stable",
                    "cmdline": [
                        "/opt/google/chrome/google-chrome-stable",
                        "--profile-directory=Default",
                    ],
                }

            def cpu_percent(self, interval=None):
                return 0.0

            def ppid(self):
                return 1

        with (
            patch("ev.tools.builtin.psutil.process_iter", return_value=[FakeProcess()]),
            patch("ev.tools.builtin.time.sleep"),
        ):
            result = get_processes({"query": "Google Chrome", "sort": "memory", "limit": 5}, None)
        self.assertEqual(result["processes"][0]["pid"], 77)
        self.assertNotIn("cmdline", result["processes"][0])

    def test_disabled_desktop_notification_stays_inside_ev(self) -> None:
        spec, arguments = self.registry.validate(
            "desktop.send_notification",
            {"title": "Test", "message": "Keep this inside E.V."},
        )
        result = spec.executor(arguments, self.registry.context)
        self.assertFalse(result["sent"])
        self.assertTrue(result["internal"])
        self.assertEqual(self.bus.history()[-1]["type"], "desktop.notification_internal")


class LocalProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_personality_changes_local_conversation_instructions(self) -> None:
        provider = LocalLlamaProvider(
            {"model": "test-model"},
            {
                "response_length": "minimal",
                "tone": "calm",
                "technical_language": "simple",
            },
        )
        instructions = provider._system_instructions()
        self.assertIn("one concise sentence", instructions)
        self.assertIn("calm, measured tone", instructions)
        self.assertIn("plain language", instructions)

    def test_casual_token_budget_is_fast_but_detail_and_tools_keep_full_budget(self) -> None:
        provider = LocalLlamaProvider(
            {"model": "test-model", "max_output_tokens": 160, "casual_max_output_tokens": 72},
            {"response_length": "normal"},
        )
        casual = [{"role": "user", "content": "How are you?"}]
        detailed = [{"role": "user", "content": "Explain that step-by-step."}]
        self.assertEqual(provider._output_token_budget(casual, []), 72)
        self.assertEqual(provider._output_token_budget(detailed, []), 160)
        self.assertEqual(provider._output_token_budget(casual, [{"name": "system.identity"}]), 160)

    def test_local_context_is_kept_for_natural_conversation(self) -> None:
        context = [
            {"role": "user", "content": "Tell me about Saturn."},
            {"role": "assistant", "content": "Its rings are mostly ice and rock."},
        ]
        self.assertEqual(
            LocalLlamaProvider._conversation_context("Why is the sky blue?", context), context
        )
        self.assertEqual(
            LocalLlamaProvider._conversation_context("How old are they?", context), context
        )
        self.assertEqual(LocalLlamaProvider._conversation_context("Tell me more", context), context)

    def test_tool_routing_does_not_match_keywords_inside_other_words(self) -> None:
        tools = [{"name": "applications.list", "permission": "SAFE"}]
        self.assertEqual(
            LocalHybridProvider._relevant_tools("Why does that happen?", [], tools), []
        )

    async def test_local_llama_parses_structured_tool_calls(self) -> None:
        provider = LocalLlamaProvider({"model": "test-model"})
        response = {
            "id": "chat-test",
            "model": "test-model",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-test",
                                "type": "function",
                                "function": {"name": "system__get_memory_usage", "arguments": "{}"},
                            }
                        ],
                    },
                }
            ],
            "usage": {"total_tokens": 12},
        }
        turn = provider._parse(
            response,
            [],
            {"system__get_memory_usage": "system.get_memory_usage"},
            time.perf_counter(),
        )
        self.assertEqual(turn.tool_calls[0].name, "system.get_memory_usage")
        self.assertEqual(turn.tool_calls[0].arguments, {})
        self.assertEqual(turn.response_id, "chat-test")

    async def test_hybrid_keeps_commands_offline_and_routes_conversation_local(self) -> None:
        provider = LocalHybridProvider({})
        local_turn = ProviderTurn("local_llama", "test-model", "Local English response")
        provider.local.begin = AsyncMock(return_value=local_turn)  # type: ignore[method-assign]

        command = await provider.begin("what is my RAM usage", [], [], [])
        self.assertEqual(command.provider, "offline")
        self.assertEqual(command.tool_calls[0].name, "system.get_memory_usage")
        provider.local.begin.assert_not_awaited()

        conversation = await provider.begin("why is the sky blue", [], [], [])
        self.assertIs(conversation, local_turn)
        provider.local.begin.assert_awaited_once_with("why is the sky blue", [], [], [])

    async def test_hybrid_keeps_normal_conversation_context(self) -> None:
        provider = LocalHybridProvider({})
        local_turn = ProviderTurn("local_llama", "test-model", "Natural follow-up")
        provider.local.begin = AsyncMock(return_value=local_turn)  # type: ignore[method-assign]
        context = [
            {"role": "user", "content": "Tell me something interesting about Saturn."},
            {"role": "assistant", "content": "Its rings are mostly ice and rock."},
        ]
        result = await provider.begin("how old are they?", context, [], [])
        self.assertIs(result, local_turn)
        provider.local.begin.assert_awaited_once_with("how old are they?", context, [], [])

    def test_hybrid_sends_only_relevant_tool_categories_to_local_model(self) -> None:
        tools = [
            {"name": "files.read"},
            {"name": "applications.open"},
            {"name": "system.get_processes"},
            {"name": "audio.set_volume"},
        ]
        self.assertEqual(LocalHybridProvider._relevant_tools("why is the sky blue", [], tools), [])
        selected = LocalHybridProvider._relevant_tools("read this file", [], tools)
        self.assertEqual([tool["name"] for tool in selected], ["files.read"])
        selected = LocalHybridProvider._relevant_tools("open this application", [], tools)
        self.assertEqual(
            [tool["name"] for tool in selected],
            ["applications.open", "system.get_processes"],
        )

    async def test_offline_routes_private_screen_capture_through_structured_tool(self) -> None:
        turn = await OfflineProvider().begin(
            "could you take a screenshot of my screen?", [], [], []
        )
        self.assertEqual(turn.tool_calls[0].name, "vision.capture")
        self.assertEqual(turn.tool_calls[0].arguments, {})


class ServiceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_service_planner_executes_internal_step_and_settles_dormant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = CarlosCore(
                paths=Paths(
                    root / "config", root / "data", root / "state", root / "cache", root / "runtime"
                )
            )
            try:
                step = service.planner._step(
                    "identity", "system.identity", {}, "Read the local system identity"
                )
                second = service.planner._step(
                    "identity_again",
                    "system.identity",
                    {},
                    "Verify the local system identity",
                    ["identity"],
                )
                plan = TaskPlan(
                    "plan", "correlation", "identify this computer", "Read identity", [step, second]
                )
                service.state.transition(CoreState.THINKING, "test", "correlation")
                result = await service.planner.execute(plan)
                self.assertEqual(result["status"], "completed")
                self.assertEqual(step.actual_result["status"], "completed")
                self.assertEqual(second.actual_result["status"], "completed")
                self.assertEqual(service.state.current, CoreState.DORMANT)
                transitions = [
                    item["payload"]["to"]
                    for item in service.bus.history()
                    if item["type"] == "core.state_changed"
                    and item.get("correlation_id") == "correlation"
                ]
                self.assertEqual(transitions, ["THINKING", "USING_TOOL", "DORMANT"])
            finally:
                service.memory.close()

    async def test_external_tool_request_cannot_enter_running_plan_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = CarlosCore(
                paths=Paths(
                    root / "config", root / "data", root / "state", root / "cache", root / "runtime"
                )
            )
            try:
                service.state.transition(CoreState.USING_TOOL, "owned by planner")
                result = await service.request_tool(
                    {"name": "system.identity", "arguments": {}}, "external"
                )
                self.assertEqual(result["status"], "busy")
                self.assertEqual(service.state.current, CoreState.USING_TOOL)
            finally:
                service.memory.close()

    async def test_personality_update_is_validated_persisted_and_applies_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = Paths(
                root / "config", root / "data", root / "state", root / "cache", root / "runtime"
            )
            service = CarlosCore(paths=paths)
            try:
                result = await service.handle_request(
                    {
                        "type": "personality.update",
                        "id": "personality-test",
                        "payload": {"tone": "calm", "voice_expressiveness": 0.8},
                    }
                )
                self.assertTrue(result["updated"])
                self.assertEqual(result["personality"]["tone"], "calm")
                self.assertEqual(service.voice.tts.config["noise_scale"], 0.8)
                stored = json.loads(paths.config_file.read_text(encoding="utf-8"))
                self.assertEqual(stored["personality"]["voice_expressiveness"], 0.8)
                self.assertEqual(paths.config_file.stat().st_mode & 0o777, 0o600)
                with self.assertRaises(ValueError):
                    service.update_personality({"tone": "unhinged"})
            finally:
                service.memory.close()

    async def test_capability_query_expands_visual_synonym_to_vision_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = CarlosCore(
                paths=Paths(
                    root / "config", root / "data", root / "state", root / "cache", root / "runtime"
                )
            )
            try:
                with (
                    patch.object(
                        service.accessibility,
                        "status",
                        return_value={"status": "LIMITED", "reason": "test"},
                    ),
                    patch.object(
                        service.vision,
                        "status",
                        return_value={
                            "available": True,
                            "semantic_understanding": True,
                            "ocr": True,
                            "pointer_control": False,
                        },
                    ),
                    patch.object(
                        service.desktop.input,
                        "status",
                        return_value={
                            "available": False,
                            "connected": False,
                            "reason": "test backend unavailable",
                        },
                    ),
                    patch.object(
                        service.coding_agent,
                        "status",
                        return_value={"available": True, "reason": "ready"},
                    ),
                ):
                    result = service.capability_query("visual")
                names = {item["name"] for item in result["matches"]}
                self.assertIn("vision.status", names)
                self.assertIn("vision.capture", names)
                self.assertEqual(
                    result["capability_gaps"][0]["required"],
                    "safe pointer input for visually located targets",
                )
            finally:
                service.memory.close()

    async def test_cancelled_waiting_plan_returns_core_to_dormant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = CarlosCore(
                paths=Paths(
                    root / "config", root / "data", root / "state", root / "cache", root / "runtime"
                )
            )
            try:
                service.state.transition(
                    CoreState.WAITING_FOR_CONFIRMATION, "test", "cancel-correlation"
                )
                with patch.object(
                    service.planner,
                    "cancel",
                    return_value={
                        "status": "cancelled",
                        "plan_id": "plan",
                        "correlation_id": "cancel-correlation",
                        "confirmation_ids": [],
                    },
                ):
                    result = await service._cancel_active_plan("user_request")
                self.assertEqual(result["status"], "cancelled")
                self.assertEqual(service.state.current, CoreState.DORMANT)
            finally:
                service.memory.close()

    async def test_panel_state_is_small_and_reflects_live_voice_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = CarlosCore(
                paths=Paths(
                    root / "config", root / "data", root / "state", root / "cache", root / "runtime"
                )
            )
            try:
                service.voice.last_input_level = {"rms": 0.125, "peak": 0.4, "waveform": []}
                service.voice.diagnostics.update(
                    {"voice_activity": True, "microphone": "test-source"}
                )
                result = await service.handle_request(
                    {"type": "panel.state", "id": "panel", "payload": {}}
                )
                self.assertEqual(result["state"], "DORMANT")
                self.assertEqual(result["rms"], 0.125)
                self.assertEqual(result["peak"], 0.4)
                self.assertTrue(result["voice_active"])
                self.assertEqual(result["microphone"], "test-source")
                self.assertNotIn("events", result)
                self.assertNotIn("tools", result)
            finally:
                service.memory.close()

    async def test_stop_phrase_immediately_ends_active_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = CarlosCore(
                paths=Paths(
                    root / "config", root / "data", root / "state", root / "cache", root / "runtime"
                )
            )
            try:
                service.voice.end_conversation = AsyncMock(return_value={"status": "conversation_ended"})  # type: ignore[method-assign]
                result = await service.handle_request(
                    {"type": "command.submit", "id": "stop-now", "payload": {"text": "shut up"}}
                )
                self.assertEqual(result["status"], "conversation_ended")
                service.voice.end_conversation.assert_awaited_once_with(
                    "user_stop_phrase", "stop-now"
                )
            finally:
                service.memory.close()

    async def test_carlos_destructive_and_coding_actions_require_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = CarlosCore(
                paths=Paths(
                    root / "config", root / "data", root / "state", root / "cache", root / "runtime"
                )
            )
            try:
                remembered = await service.request_tool(
                    {
                        "name": "memory.remember",
                        "arguments": {"content": "trusted test", "tags": []},
                    },
                    "trusted-sensitive",
                )
                self.assertEqual(remembered["status"], "completed")
                memory_id = remembered["result"]["memory"]["id"]
                destructive = await service.request_tool(
                    {"name": "memory.forget", "arguments": {"id": memory_id}},
                    "trusted-destructive",
                )
                self.assertEqual(destructive["status"], "confirmation_required")
                self.assertTrue(any(m["id"] == memory_id for m in service.memory.list_memories()))
                pending = await service.request_tool(
                    {
                        "name": "development.coding_agent_execute",
                        "arguments": {"proposal_id": "a" * 32},
                    },
                    "coding-execute",
                )
                self.assertEqual(pending["status"], "confirmation_required")
                self.assertEqual(
                    pending["confirmation"]["tool"], "development.coding_agent_execute"
                )
                self.assertEqual(pending["confirmation"]["permission"], "HIGH")
            finally:
                service.memory.close()

    async def test_direct_tool_request_during_voice_pipeline_returns_busy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = CarlosCore(
                paths=Paths(
                    root / "config", root / "data", root / "state", root / "cache", root / "runtime"
                )
            )
            try:
                service.state.transition(CoreState.LISTENING, "test")
                result = await service.request_tool(
                    {"name": "system.get_memory_usage", "arguments": {}},
                    "busy-tool",
                )
                self.assertEqual(result["status"], "busy")
                self.assertEqual(service.state.current, CoreState.LISTENING)
                self.assertTrue(
                    any(event["type"] == "tool.deferred" for event in service.bus.history())
                )
            finally:
                service.memory.close()

    async def test_transcribed_planned_command_enters_thinking_before_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = CarlosCore(
                paths=Paths(
                    root / "config", root / "data", root / "state", root / "cache", root / "runtime"
                )
            )
            try:
                service.state.transition(CoreState.LISTENING, "test")
                service.state.transition(CoreState.TRANSCRIBING, "test")
                fake_plan = SimpleNamespace(timings={})
                service.planner.try_plan = MagicMock(return_value=fake_plan)  # type: ignore[method-assign]

                async def execute(_plan):
                    self.assertEqual(service.state.current, CoreState.THINKING)
                    service.state.transition(CoreState.DORMANT, "done")
                    return {"status": "completed", "response": "Done."}

                service.planner.execute = execute  # type: ignore[method-assign]
                result = await service._submit_action_clauses(
                    "move Firefox to the other monitor", "voice-plan"
                )
                self.assertEqual(result["status"], "completed")
                self.assertEqual(service.state.current, CoreState.DORMANT)
            finally:
                service.memory.close()

    async def test_expired_confirmation_clears_command_and_returns_dormant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = CarlosCore(
                paths=Paths(
                    root / "config", root / "data", root / "state", root / "cache", root / "runtime"
                )
            )
            try:
                pending = service.permissions.create(
                    "applications.close_process",
                    {"pid": 123, "started_at_epoch": 42.0},
                    Permission.SENSITIVE,
                    "test",
                    "expired-correlation",
                )
                pending.expires_monotonic = time.monotonic() - 1
                service.brain.pending[pending.id] = object()  # type: ignore[assignment]
                service.state.transition(
                    CoreState.WAITING_FOR_CONFIRMATION, "test", pending.correlation_id
                )
                self.assertEqual(await service._prune_expired_confirmations(), 1)
                self.assertNotIn(pending.id, service.brain.pending)
                self.assertEqual(service.state.current, CoreState.DORMANT)
                expired_events = [
                    event
                    for event in service.bus.history()
                    if event["type"] == "tool.permission_check"
                    and event["payload"].get("decision") == "EXPIRED"
                ]
                self.assertEqual(len(expired_events), 1)
            finally:
                service.memory.close()

    async def test_completed_command_retrieves_memory_and_schedules_tts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = Paths(
                root / "config", root / "data", root / "state", root / "cache", root / "runtime"
            )
            service = CarlosCore(paths=paths)
            service.voice.speak = AsyncMock(return_value={"status": "completed"})  # type: ignore[method-assign]
            with patch.object(
                VoiceManager, "tts_available", new_callable=PropertyMock, return_value=True
            ):
                result = await service.handle_request(
                    {"type": "command.submit", "id": "spoken", "payload": {"text": "hello E.V."}}
                )
                await asyncio.gather(*tuple(service._background_tasks))

            self.assertEqual(result["status"], "completed")
            service.voice.speak.assert_awaited_once_with(
                result["response"], result["correlation_id"], allow_follow_up=True
            )
            transitions = [
                event["payload"]["to"]
                for event in service.bus.history()
                if event["type"] == "core.state_changed"
            ]
            self.assertIn("RETRIEVING_MEMORY", transitions)
            service.memory.close()

    async def test_private_ipc_health_snapshot_and_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = Paths(
                root / "config", root / "data", root / "state", root / "cache", root / "runtime"
            )
            service = CarlosCore(paths=paths)
            # This tests the private socket, not ownership of the live desktop's
            # singleton D-Bus name. Never contend with the user's running E.V.
            service.claim_dbus_activation_name = lambda: None
            service.config["assistant"]["speak_responses"] = False
            task = asyncio.create_task(service.run())
            for _ in range(100):
                if paths.socket.exists():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(paths.socket.exists())
            self.assertEqual(paths.socket.stat().st_mode & 0o777, 0o600)

            reader, writer = await asyncio.open_unix_connection(paths.socket, limit=1_048_576)
            hello = json.loads(await reader.readline())
            self.assertEqual(hello["type"], "hello")

            async def request(kind: str, payload: dict | None = None) -> dict:
                writer.write(encode_message({"type": kind, "id": kind, "payload": payload or {}}))
                await writer.drain()
                return json.loads(await reader.readline())

            health = await request("health")
            self.assertTrue(health["payload"]["ok"])
            snapshot = await request("snapshot")
            self.assertEqual(snapshot["payload"]["core"]["state"], "DORMANT")
            self.assertGreaterEqual(snapshot["payload"]["tools"]["registered"], 25)
            tools = await request("tool.catalog")
            self.assertTrue(
                any(item["name"] == "system.get_memory_usage" for item in tools["payload"]["tools"])
            )
            tool_result = await request(
                "tool.call", {"name": "system.get_memory_usage", "arguments": {}}
            )
            self.assertEqual(tool_result["payload"]["status"], "completed")
            self.assertIn("memory", tool_result["payload"]["result"])
            command = await request("command.submit", {"text": "how much RAM is used?"})
            self.assertEqual(command["payload"]["status"], "completed")
            self.assertIn("RAM is", command["payload"]["response"])
            remembered = await request(
                "memory.remember", {"content": "Remember integration", "tags": ["test"]}
            )
            self.assertEqual(remembered["payload"]["status"], "completed")
            memory_id = remembered["payload"]["result"]["memory"]["id"]
            listed = await request("memory.list")
            self.assertEqual(listed["payload"]["memories"][0]["id"], memory_id)

            writer.close()
            await writer.wait_closed()
            service.stop_event.set()
            await asyncio.wait_for(task, 3)


if __name__ == "__main__":
    unittest.main()
