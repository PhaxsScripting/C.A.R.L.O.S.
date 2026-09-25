from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "core"))

from ev.events import PhaxEventBus  # noqa: E402
from ev.state import StateMachine  # noqa: E402
from ev.voice import VoiceManager  # noqa: E402


class FakeProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.stopped = asyncio.Event()
        self.stdout = object()
        self.stderr = None

    def terminate(self) -> None:
        self.returncode = -15
        self.stopped.set()

    def kill(self) -> None:
        self.returncode = -9
        self.stopped.set()

    async def wait(self) -> int:
        await self.stopped.wait()
        return int(self.returncode or 0)


class DefaultSourceFollowTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def manager(source: str = "@DEFAULT_SOURCE@") -> VoiceManager:
        bus = PhaxEventBus()
        return VoiceManager(
            {
                "microphone_source": source,
                "capture_command": ["/usr/bin/parec", "--raw"],
                "wake": {"enabled": True, "source_poll_seconds": 0.1},
            },
            bus,
            StateMachine(bus),
        )

    async def test_default_source_change_restarts_ambient_capture(self) -> None:
        manager = self.manager()
        first = FakeProcess()
        second = FakeProcess()
        sources = iter(("mic-old", "mic-new", "mic-new"))

        async def current_source() -> str:
            return next(sources, "mic-new")

        async def audio_loop(process: FakeProcess) -> None:
            await process.stopped.wait()

        manager._current_source = current_source  # type: ignore[method-assign]
        manager._wake_audio_loop = audio_loop  # type: ignore[method-assign]
        manager.wake.start = AsyncMock()  # type: ignore[method-assign]
        manager.wake.stop = AsyncMock()  # type: ignore[method-assign]

        with patch(
            "ev.voice.manager.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=(first, second)),
        ) as create_process:
            supervisor = asyncio.create_task(manager._wake_supervisor())
            try:
                for _ in range(40):
                    started = [
                        event
                        for event in manager.bus.history()
                        if event["type"] == "wake.listening_started"
                    ]
                    if len(started) >= 2:
                        break
                    await asyncio.sleep(0.025)
                self.assertGreaterEqual(len(started), 2)
            finally:
                manager.wake_desired = False
                supervisor.cancel()
                await asyncio.gather(supervisor, return_exceptions=True)

        commands = [call.args for call in create_process.await_args_list]
        self.assertIn("--device=mic-old", commands[0])
        self.assertIn("--device=mic-new", commands[1])
        changed = [
            event for event in manager.bus.history() if event["type"] == "wake.source_changed"
        ]
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["payload"]["previous_source"], "mic-old")
        self.assertEqual(changed[0]["payload"]["source"], "mic-new")
        self.assertTrue(changed[0]["payload"]["automatic"])

    async def test_source_change_waits_until_active_capture_finishes(self) -> None:
        manager = self.manager()
        manager.capture_active = True
        manager._current_source = AsyncMock(return_value="mic-new")  # type: ignore[method-assign]
        watcher = asyncio.create_task(manager._wait_for_default_source_change("mic-old"))
        await asyncio.sleep(0.15)
        self.assertFalse(watcher.done())
        manager.capture_active = False
        self.assertEqual(await asyncio.wait_for(watcher, timeout=0.5), "mic-new")

    async def test_explicit_source_is_never_followed(self) -> None:
        manager = self.manager("studio-mic")
        manager._current_source = AsyncMock(return_value="other-mic")  # type: ignore[method-assign]
        self.assertFalse(manager._follows_default_source)
        self.assertEqual(await manager._wait_for_default_source_change("studio-mic"), "")
        manager._current_source.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
