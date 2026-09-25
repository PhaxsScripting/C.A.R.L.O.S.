from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from ev.brain import CommandEngine
from ev.events import PhaxEventBus
from ev.state import CoreState, StateMachine
from ev.voice import SynthesizedAudio, VoiceManager
from ev.voice.tts import PiperAdapter


class VoicePlaybackRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bus = PhaxEventBus()
        self.manager = VoiceManager(
            {"capture_command": ["/missing"], "follow_up": {"enabled": False}},
            self.bus,
            StateMachine(self.bus),
        )
        self.audio = SynthesizedAudio(b"\0\0" * 22050, 22050, 1, 2, "test", "test", 1.0)
        self.manager.tts.synthesize = AsyncMock(return_value=self.audio)
        self.available = patch.object(
            VoiceManager, "tts_available", new_callable=PropertyMock, return_value=True
        )
        self.available.start()
        self.addCleanup(self.available.stop)

    def assert_ready(self) -> None:
        self.assertFalse(self.manager.speech_pending)
        self.assertFalse(self.manager.speaking)
        self.assertIsNone(self.manager.tts_process)
        self.assertEqual(self.manager.state.current, CoreState.DORMANT)

    async def test_entire_audio_is_buffered_without_wall_clock_chunk_sleeps(self) -> None:
        player = Mock(returncode=0, stdin=Mock())
        player.communicate = AsyncMock(return_value=(None, b""))
        with patch(
            "ev.voice.manager.asyncio.create_subprocess_exec", AsyncMock(return_value=player)
        ):
            result = await asyncio.wait_for(self.manager.speak("ready", "buffered"), timeout=0.5)
        player.communicate.assert_awaited_once_with(self.audio.pcm)
        player.stdin.write.assert_not_called()
        self.assertEqual(result["status"], "completed")
        self.assert_ready()

    async def test_spawn_failure_clears_pending_speech(self) -> None:
        with patch(
            "ev.voice.manager.asyncio.create_subprocess_exec",
            AsyncMock(side_effect=OSError("no output device")),
        ):
            with self.assertRaises(OSError):
                await self.manager.speak("ready", "spawn-error")
        self.assert_ready()
        self.assertEqual(self.manager.diagnostics["tts_state"], "FAILED")

    async def test_playback_error_stops_player_and_releases_state(self) -> None:
        player = Mock(returncode=1, stdin=Mock())
        player.stdin.is_closing.return_value = False
        player.communicate = AsyncMock(return_value=(None, b"audio device disconnected"))
        player.wait = AsyncMock(return_value=1)
        with patch(
            "ev.voice.manager.asyncio.create_subprocess_exec", AsyncMock(return_value=player)
        ):
            with self.assertRaisesRegex(RuntimeError, "audio device disconnected"):
                await self.manager.speak("ready", "playback-error")
        player.stdin.close.assert_called_once()
        self.assert_ready()

    async def test_cancel_during_playback_terminates_player(self) -> None:
        started = asyncio.Event()
        player = Mock(returncode=None, stdin=Mock())
        player.stdin.is_closing.return_value = False
        player.wait = AsyncMock(return_value=-15)

        async def communicate(_pcm):
            started.set()
            await asyncio.Event().wait()

        player.communicate = communicate
        with patch(
            "ev.voice.manager.asyncio.create_subprocess_exec", AsyncMock(return_value=player)
        ):
            task = asyncio.create_task(self.manager.speak("ready", "playback-cancelled"))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        player.terminate.assert_called_once()
        self.assert_ready()

    async def test_cancel_during_synthesis_does_not_leave_barge_in_stuck(self) -> None:
        started = asyncio.Event()

        async def synthesize(_text: str):
            started.set()
            await asyncio.Event().wait()

        self.manager.tts.synthesize = synthesize
        task = asyncio.create_task(self.manager.speak("ready", "cancelled"))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_ready()

    async def test_invalid_audio_clears_pending_speech(self) -> None:
        self.manager.tts.synthesize = AsyncMock(
            return_value=SynthesizedAudio(b"", 0, 1, 2, "test", "test", 1.0)
        )
        with self.assertRaises(RuntimeError):
            await self.manager.speak("ready", "invalid")
        self.assert_ready()

    async def test_microphone_eof_finishes_capture_and_recovers(self) -> None:
        process = Mock(returncode=0)
        process.stdout.read = AsyncMock(return_value=b"")
        process.wait = AsyncMock(return_value=0)
        process.stderr.read = AsyncMock(return_value=b"")
        self.manager.capture_process = process
        await self.manager._activate_capture("eof", "command", "manual", "test")
        await self.manager._capture_loop("eof")
        await asyncio.wait_for(self.manager.auto_stop_task, timeout=1)
        self.assertFalse(self.manager.capture_active)
        self.assertEqual(self.manager.state.current, CoreState.DORMANT)
        self.assertEqual(self.manager.diagnostics["capture_health"], "DEVICE_ERROR")

    async def test_microphone_stall_reaches_recovery(self) -> None:
        process = Mock(returncode=0)
        process.stdout.read = AsyncMock(side_effect=TimeoutError("stalled"))
        process.wait = AsyncMock(return_value=0)
        process.stderr.read = AsyncMock(return_value=b"")
        self.manager.capture_process = process
        await self.manager._activate_capture("stall", "command", "manual", "test")
        await self.manager._capture_loop("stall")
        await asyncio.wait_for(self.manager.auto_stop_task, timeout=1)
        self.assertFalse(self.manager.capture_active)
        self.assertEqual(self.manager.state.current, CoreState.DORMANT)

    async def test_unexpected_command_failure_releases_wake_state(self) -> None:
        provider = Mock(name="test", model="test")
        provider.begin = AsyncMock(side_effect=RuntimeError("unexpected tool failure"))
        memory = Mock()
        memory.list_memories.return_value = []
        memory.recent_conversation.return_value = [{"content": "hello"}]
        engine = CommandEngine(provider, self.bus, self.manager.state, memory, [], AsyncMock())
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                await engine.submit("hello", "failure")
            self.assertEqual(self.manager.state.current, CoreState.DORMANT)
            self.assertFalse(engine._command_lock.locked())

    async def test_cancelled_piper_response_cannot_desynchronize_next_response(self) -> None:
        adapter = PiperAdapter({})
        adapter._synthesize_persistent = AsyncMock(side_effect=asyncio.CancelledError())
        adapter._synthesize_oneshot = AsyncMock()
        adapter.close = AsyncMock()
        with patch.object(
            PiperAdapter, "available", new_callable=PropertyMock, return_value=(True, "ready")
        ):
            with self.assertRaises(asyncio.CancelledError):
                await adapter.synthesize("cancelled")
        adapter.close.assert_awaited_once()
        adapter._synthesize_oneshot.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
