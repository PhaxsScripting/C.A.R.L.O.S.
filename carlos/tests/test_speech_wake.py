from __future__ import annotations
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from ev.voice.speech_wake import SpeechWakeFallback, _NAME
from ev.voice.normalization import normalize_transcript
from ev.voice.manager import VoiceManager
from ev.events import PhaxEventBus
from ev.state import StateMachine, CoreState


class SpeechWakeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.allowed = True
        self.vad = SimpleNamespace(analyze=AsyncMock(return_value=0.0))
        self.stt = SimpleNamespace(
            transcribe=AsyncMock(return_value=SimpleNamespace(raw="Eevee, what time is it?"))
        )
        self.detected = AsyncMock()
        self.wake = SpeechWakeFallback(self.vad, self.stt, lambda: self.allowed, self.detected)

    def test_exact_leading_name_only(self):
        for text in (
            "Eevee",
            "E.V. what time is it?",
            "Hey, Eevee",
            "Evie, hello",
            "Eve",
            "Hey Eve, hello",
        ):
            self.assertIsNotNone(_NAME.match(text), text)
        for text in (
            "Every evening",
            "Evening",
            "Event",
            "Believe me",
            "TV is on",
            "I was talking about Eevee",
            "I told Eve",
            "Evan",
            "EVs are electric cars",
        ):
            self.assertIsNone(_NAME.match(text), text)
        self.assertEqual(
            normalize_transcript("Hey, Eevee, Eevee. What time is it?"), "What time is it?"
        )
        self.assertEqual(normalize_transcript("Eevee, Eevee, Eevee."), "")

    async def test_silence_never_runs_stt(self):
        for _ in range(35):
            await self.wake.feed(bytes(3200))
        self.stt.transcribe.assert_not_awaited()
        self.assertLessEqual(sum(map(len, self.wake.pre_roll)), 9600)

    async def test_speech_end_checks_name_and_preserves_command_seed(self):
        self.vad.analyze.return_value = 0.9
        for _ in range(5):
            await self.wake.feed(bytes(3200))
        self.vad.analyze.return_value = 0.0
        for _ in range(4):
            await self.wake.feed(bytes(3200))
        await self.wake.task
        self.detected.assert_awaited_once()
        message = self.detected.await_args.args[0]
        self.assertTrue(message["seed_is_command"])
        self.assertEqual(message["source"], "local_speech_backup")
        self.assertLessEqual(len(message["seed_pcm"]), 192000)
        await self.wake.feed(bytes(3200))
        self.assertEqual(self.stt.transcribe.await_count, 1)

    async def test_name_only_does_not_finish_command_capture(self):
        self.stt.transcribe.return_value.raw = "Hey, Eevee."
        await self.wake._check(bytes(16000))
        self.assertFalse(self.detected.await_args.args[0]["seed_is_command"])

    async def test_eve_pronunciation_wakes_and_retains_following_command(self):
        for raw, has_command in (("Eve.", False), ("Hey Eve, what time is it?", True)):
            with self.subTest(raw=raw):
                self.detected.reset_mock()
                self.stt.transcribe.return_value.raw = raw
                await self.wake._check(bytes(16000))
                self.detected.assert_awaited_once()
                self.assertEqual(self.detected.await_args.args[0]["seed_is_command"], has_command)

    async def test_short_quiet_name_gets_transcribed_but_still_requires_name(self):
        self.vad.analyze.return_value = 0.5
        await self.wake.feed(bytes(3200))
        self.vad.analyze.return_value = 0.0
        for _ in range(4):
            await self.wake.feed(bytes(3200))
        await self.wake.task
        self.detected.assert_awaited_once()
        self.assertEqual(self.wake.snapshot()["speech_frames"], 1)
        self.assertEqual(self.wake.snapshot()["frames_analyzed"], 5)

    async def test_quiet_unrelated_speech_cannot_authorize_a_wake(self):
        self.stt.transcribe.return_value.raw = "Every evening."
        self.vad.analyze.return_value = 0.5
        await self.wake.feed(bytes(3200))
        self.vad.analyze.return_value = 0.0
        for _ in range(4):
            await self.wake.feed(bytes(3200))
        await self.wake.task
        self.detected.assert_not_awaited()

    def test_loud_noise_warning_requires_sustained_noise_not_quiet_room(self):
        bus = PhaxEventBus()
        manager = VoiceManager({"wake": {"enabled": True}}, bus, StateMachine(bus))
        manager.speech_wake.last_probability = 0.01
        for _ in range(100):
            manager._update_wake_input_quality(0.001, 100)
        self.assertEqual(manager.diagnostics["wake_input_quality"], "MONITORING")
        for _ in range(100):
            manager._update_wake_input_quality(0.2, 100)
        self.assertEqual(manager.diagnostics["wake_input_quality"], "LOUD_NON_SPEECH")
        self.assertEqual(sum(e["type"] == "wake.input_quality_changed" for e in bus.history()), 1)
        manager.speech_wake.last_probability = 0.9
        for _ in range(20):
            manager._update_wake_input_quality(0.2, 100)
        self.assertEqual(manager.diagnostics["wake_input_quality"], "MONITORING")

    async def test_unrelated_speech_does_not_wake(self):
        self.stt.transcribe.return_value.raw = "Believe me, this is great."
        await self.wake._check(bytes(16000))
        self.detected.assert_not_awaited()

    async def test_stale_result_cannot_wake_after_state_changes(self):
        started, finish = asyncio.Event(), asyncio.Event()

        async def transcribe(pcm):
            started.set()
            await finish.wait()
            return SimpleNamespace(raw="Eevee")

        self.stt.transcribe.side_effect = transcribe
        task = asyncio.create_task(self.wake._check(bytes(16000)))
        await started.wait()
        self.allowed = False
        finish.set()
        await task
        self.detected.assert_not_awaited()

    async def test_pending_check_is_cancelled_and_buffers_cleared(self):
        async def slow_transcribe(_pcm):
            await asyncio.sleep(30)
            return SimpleNamespace(raw="Eevee")

        self.stt.transcribe.side_effect = slow_transcribe
        self.wake.task = asyncio.create_task(self.wake._check(bytes(16000)))
        await asyncio.sleep(0)
        await self.wake.close()
        self.assertIsNone(self.wake.task)
        self.assertFalse(self.wake.pcm)
        self.assertFalse(self.wake.tail)

    async def test_manager_disables_backup_during_capture_privacy_and_thinking(self):
        bus = PhaxEventBus()
        manager = VoiceManager(
            {"wake": {"enabled": True, "speech_backup": True}}, bus, StateMachine(bus)
        )
        self.assertTrue(manager._speech_wake_allowed())
        manager.privacy_mode = True
        self.assertFalse(manager._speech_wake_allowed())
        manager.privacy_mode = False
        manager.capture_active = True
        self.assertFalse(manager._speech_wake_allowed())
        manager.capture_active = False
        manager.state.transition(CoreState.AWAKE, "test")
        self.assertFalse(manager._speech_wake_allowed())

    async def test_completed_wake_command_seed_is_not_discarded_as_name_only(self):
        from array import array
        import math

        bus = PhaxEventBus()
        manager = VoiceManager(
            {"wake": {"enabled": False}, "vad": {"minimum_rms": 0.001, "start_ms": 100}},
            bus,
            StateMachine(bus),
        )
        pcm = array("h", (int(1000 * math.sin(i * 0.1)) for i in range(16000))).tobytes()
        await manager._activate_capture(
            "test", "wake_command", "ambient", "test", pcm, seed_is_command=True
        )
        self.assertIsNotNone(manager.capture_speech_start_byte)
        self.assertEqual(manager.capture_seed_bytes, 0)
        await manager.abort_capture("test")
