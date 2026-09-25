from __future__ import annotations

import asyncio
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, PropertyMock, patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "core"))

from ev.events import PhaxEventBus  # noqa: E402
from ev.state import CoreState, StateMachine  # noqa: E402
from ev.voice import SynthesizedAudio, VoiceManager  # noqa: E402


class FakePlayer:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = False
        self.stopped = asyncio.Event()

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self.stopped.set()

    def kill(self) -> None:
        self.returncode = -9
        self.stopped.set()

    async def wait(self) -> int:
        await self.stopped.wait()
        return int(self.returncode or 0)


class WakePendingSpeechTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def manager() -> VoiceManager:
        bus = PhaxEventBus()
        manager = VoiceManager(
            {
                "capture_command": ["/missing"],
                "wake": {"enabled": True},
                "follow_up": {"enabled": False},
            },
            bus,
            StateMachine(bus),
        )
        manager.diagnostics["microphone"] = "test-microphone"
        manager.wake_last_feed_at = time.monotonic()
        return manager

    async def test_wake_cancels_tts_blocked_while_player_is_starting(self) -> None:
        manager = self.manager()
        player = FakePlayer()
        player_spawn_started = asyncio.Event()
        release_player_spawn = asyncio.Event()
        audio = SynthesizedAudio(b"\0\0", 22050, 1, 2, "test", "test", 1.0)
        manager.tts.synthesize = AsyncMock(return_value=audio)  # type: ignore[method-assign]

        async def delayed_player(*_args: object, **_kwargs: object) -> FakePlayer:
            player_spawn_started.set()
            await release_player_spawn.wait()
            return player

        with (
            patch.object(
                VoiceManager, "tts_available", new_callable=PropertyMock, return_value=True
            ),
            patch("ev.voice.manager.asyncio.create_subprocess_exec", side_effect=delayed_player),
        ):
            speech_task = asyncio.create_task(manager.speak("done", "speech"))
            await player_spawn_started.wait()
            self.assertTrue(manager.speech_pending)
            self.assertEqual(manager.state.current, CoreState.DORMANT)

            await manager._on_wake_detected({"keyword": "E_V"})
            self.assertTrue(manager.capture_active)
            self.assertEqual(manager.state.current, CoreState.LISTENING)

            release_player_spawn.set()
            result = await asyncio.wait_for(speech_task, timeout=1)

        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["reason"], "wake_word_barge_in")
        self.assertTrue(player.terminated)
        self.assertFalse(manager.speech_pending)
        self.assertFalse(manager.speaking)
        self.assertEqual(manager.state.current, CoreState.LISTENING)
        self.assertTrue(any(event["type"] == "voice.barge_in" for event in manager.bus.history()))
        await manager.abort_capture("test_cleanup")

    async def test_wake_treats_speaking_flag_as_barge_in_before_capture(self) -> None:
        manager = self.manager()
        manager.speaking = True
        original_stop = manager.stop_speaking
        manager.stop_speaking = AsyncMock(wraps=original_stop)  # type: ignore[method-assign]

        await manager._on_wake_detected({"keyword": "E_V"})

        manager.stop_speaking.assert_awaited_once()
        self.assertTrue(manager.capture_active)
        self.assertEqual(manager.state.current, CoreState.LISTENING)
        await manager.abort_capture("test_cleanup")

    async def test_wake_phrase_seed_cannot_end_command_capture(self) -> None:
        bus = PhaxEventBus()
        manager = VoiceManager(
            {
                "capture_command": ["/missing"],
                "wake": {"enabled": True, "command_wait_seconds": 2.0},
                "vad": {"start_ms": 50, "minimum_speech_ms": 50, "end_silence_ms": 100},
            },
            bus,
            StateMachine(bus),
        )
        wake_phrase = int(5000).to_bytes(2, "little", signed=True) * 1600
        trailing_silence = b"\0\0" * 2400

        await manager._activate_capture(
            "wake", "wake_command", "ambient", "test-microphone", wake_phrase + trailing_silence
        )

        self.assertTrue(manager.capture_active)
        self.assertIsNone(manager.capture_speech_start_byte)
        self.assertIsNone(manager.auto_stop_task)
        self.assertEqual(manager.capture_seed_bytes, len(wake_phrase + trailing_silence))
        await manager.abort_capture("test_cleanup")

    async def test_post_wake_speech_arms_and_ends_command_capture(self) -> None:
        bus = PhaxEventBus()
        manager = VoiceManager(
            {
                "capture_command": ["/missing"],
                "wake": {"enabled": True, "command_wait_seconds": 2.0},
                "vad": {"start_ms": 50, "minimum_speech_ms": 50, "end_silence_ms": 100},
            },
            bus,
            StateMachine(bus),
        )
        seed = int(5000).to_bytes(2, "little", signed=True) * 800 + b"\0\0" * 1600
        await manager._activate_capture("wake", "wake_command", "ambient", "test-microphone", seed)

        speech = int(5000).to_bytes(2, "little", signed=True) * 800
        self.assertFalse(manager._consume_capture_chunk(speech))
        self.assertIsNotNone(manager.capture_speech_start_byte)
        self.assertFalse(manager._consume_capture_chunk(b"\0\0" * 800))
        self.assertTrue(manager._consume_capture_chunk(b"\0\0" * 800))
        self.assertEqual(manager.capture_auto_reason, "end_of_speech")
        await manager.abort_capture("test_cleanup")

    async def test_wake_test_expiry_reports_signal_and_cleans_state(self) -> None:
        manager = self.manager()
        manager.wake.process = FakePlayer()  # type: ignore[assignment]
        manager.diagnostics["wake_state"] = "ACTIVE"
        result = manager.arm_wake_test(0.1)
        self.assertEqual(result["timeout_seconds"], 3.0)
        deadline = manager.wake_test_armed_until
        manager.wake_test_max_rms = 0.02
        manager.wake_test_max_peak = 0.08
        if manager.wake_test_task is not None:
            manager.wake_test_task.cancel()
            await asyncio.gather(manager.wake_test_task, return_exceptions=True)
            manager.wake_test_task = None

        await manager._expire_wake_test(deadline, 0.0)

        failures = [event for event in manager.bus.history() if event["type"] == "wake.test_failed"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["payload"]["reason"], "keyword_not_recognized")
        self.assertTrue(failures[0]["payload"]["signal_detected"])
        self.assertEqual(manager.wake_test_armed_until, 0.0)

    async def test_wake_detection_cancels_expiry_and_reports_levels(self) -> None:
        manager = self.manager()
        manager.wake.process = FakePlayer()  # type: ignore[assignment]
        manager.wake_test_armed_until = time.monotonic() + 30
        manager.wake_test_max_rms = 0.03
        manager.wake_test_max_peak = 0.12
        manager.wake_test_task = asyncio.create_task(asyncio.sleep(30))  # type: ignore[assignment]

        await manager._on_wake_detected({"keyword": "E_V"})
        await asyncio.sleep(0)

        completions = [
            event for event in manager.bus.history() if event["type"] == "wake.test_complete"
        ]
        self.assertEqual(len(completions), 1)
        self.assertEqual(completions[0]["payload"]["max_input_rms"], 0.03)
        self.assertTrue(manager.wake_test_task is None)
        self.assertEqual(manager.wake_test_armed_until, 0.0)

    async def test_empty_wake_command_restores_active_wake_diagnostic(self) -> None:
        manager = self.manager()
        manager.wake.process = FakePlayer()  # type: ignore[assignment]
        manager.wake_audio_process = FakePlayer()  # type: ignore[assignment]
        manager.capture_active = True
        manager.capture_origin = "ambient"
        manager.capture_mode = "wake_command"
        manager.capture_correlation_id = "wake-empty"
        manager.capture_started = time.monotonic()
        manager.capture_bytes = 3200
        manager.capture_pcm.extend(b"\0" * 3200)
        manager.diagnostics["wake_state"] = "DETECTED"

        result = await manager.stop_capture("wake-empty")

        self.assertEqual(result["status"], "no_speech")
        self.assertEqual(manager.diagnostics["wake_state"], "ACTIVE")

    async def test_wake_recovers_from_offline_provider_state(self) -> None:
        manager = self.manager()
        manager.state.transition(CoreState.OFFLINE, "provider failed")

        await manager._on_wake_detected({"keyword": "E_V"})

        self.assertTrue(manager.capture_active)
        self.assertEqual(manager.state.current, CoreState.LISTENING)
        await manager.abort_capture("test_cleanup")


if __name__ == "__main__":
    unittest.main()


class SpeechBargeInTests(unittest.IsolatedAsyncioTestCase):
    async def test_ordinary_speech_requires_echo_reference_and_two_frames(self):
        manager = WakePendingSpeechTests.manager()
        manager.config["speech_barge_in"] = True
        manager.speaking = True
        manager.neural_vad.analyze = AsyncMock(return_value=0.95)
        manager._on_wake_detected = AsyncMock()
        await manager._speech_barge_in(b"\0" * 3200)
        manager._on_wake_detected.assert_not_called()
        manager.echo.process = FakePlayer()
        manager.echo.state = "ACTIVE"
        await manager._speech_barge_in(b"\0" * 3200)
        manager._on_wake_detected.assert_not_called()
        await manager._speech_barge_in(b"\0" * 3200)
        manager._on_wake_detected.assert_awaited_once()
        self.assertEqual(manager._on_wake_detected.call_args.args[0]["source"], "aec_speech")

    async def test_mute_cannot_barge_in(self):
        manager = WakePendingSpeechTests.manager()
        manager.config["speech_barge_in"] = True
        manager.speaking = True
        manager.privacy_mode = True
        manager.echo.process = FakePlayer()
        manager.echo.state = "ACTIVE"
        manager.neural_vad.analyze = AsyncMock(return_value=1)
        await manager._speech_barge_in(b"\0" * 16000)
        manager.neural_vad.analyze.assert_not_called()
