import asyncio
import sys
import unittest
from unittest.mock import AsyncMock, PropertyMock, patch

from ev.voice.wake import WakeWordWorker


class WakeStartupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.children = []
        self.worker = WakeWordWorker({"python": sys.executable, "load_timeout_seconds": 1})
        self.native_spawn = asyncio.create_subprocess_exec
        self.available = patch.object(
            WakeWordWorker, "available", new_callable=PropertyMock, return_value=(True, "fixture")
        )
        self.available.start()

    async def asyncTearDown(self):
        await self.worker.stop()
        for child in self.children:
            if child.returncode is None:
                child.kill()
            await child.wait()
        self.available.stop()

    async def spawn_fixture(self, program):
        child = await self.native_spawn(
            sys.executable,
            "-u",
            "-c",
            program,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.children.append(child)
        return child

    async def test_invalid_handshake_reaps_real_child_and_allows_next_start(self):
        for line in ("not json", "[]", '{"type":"error"}'):

            async def spawn(*args, **kwargs):
                return await self.spawn_fixture(
                    f"import time; print({line!r}, flush=True); time.sleep(20)"
                )

            with patch("ev.voice.wake.asyncio.create_subprocess_exec", side_effect=spawn):
                with self.assertRaises((ValueError, RuntimeError)):
                    await self.worker.start(AsyncMock(), lambda *args: None)
            self.assertFalse(self.worker.running)
            self.assertIsNone(self.worker.reader_task)
            self.assertIsNone(self.worker.stderr_task)
            self.assertIsNotNone(self.children[-1].returncode)

    async def test_cancel_during_readiness_reaps_child(self):
        spawned = asyncio.Event()

        async def spawn(*args, **kwargs):
            child = await self.spawn_fixture("import time; time.sleep(20)")
            spawned.set()
            return child

        with patch("ev.voice.wake.asyncio.create_subprocess_exec", side_effect=spawn):
            task = asyncio.create_task(self.worker.start(AsyncMock(), lambda *args: None))
            await spawned.wait()
            await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
        self.assertIsNotNone(self.children[-1].returncode)
        self.assertFalse(self.worker.running)

    async def test_cancel_during_spawn_does_not_lose_real_child_handle(self):
        spawned, release = asyncio.Event(), asyncio.Event()

        async def spawn(*args, **kwargs):
            child = await self.spawn_fixture("import time; time.sleep(20)")
            spawned.set()
            await release.wait()
            return child

        with patch("ev.voice.wake.asyncio.create_subprocess_exec", side_effect=spawn):
            task = asyncio.create_task(self.worker.start(AsyncMock(), lambda *args: None))
            await spawned.wait()
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
        self.assertIsNotNone(self.children[-1].returncode)
        self.assertFalse(self.worker.running)

    async def test_stderr_flood_cannot_deadlock_readiness_and_listener_can_restart(self):
        async def spawn(*args, **kwargs):
            return await self.spawn_fixture(
                'import sys,time; sys.stderr.write("diagnostic" * 40000); sys.stderr.flush(); print(\'{"type":"ready"}\', flush=True); time.sleep(20)'
            )

        with patch("ev.voice.wake.asyncio.create_subprocess_exec", side_effect=spawn):
            for _ in range(2):
                await asyncio.wait_for(self.worker.start(AsyncMock(), lambda *args: None), 3)
                self.assertTrue(self.worker.running)
                self.assertFalse(self.worker.reader_task.done())
                await self.worker.stop()
                self.assertIsNotNone(self.children[-1].returncode)

    async def test_startup_timeout_does_not_leave_nominally_running_listener(self):
        self.worker.config["load_timeout_seconds"] = 0.05

        async def spawn(*args, **kwargs):
            return await self.spawn_fixture("import time; time.sleep(20)")

        with patch("ev.voice.wake.asyncio.create_subprocess_exec", side_effect=spawn):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                await self.worker.start(AsyncMock(), lambda *args: None)
        self.assertFalse(self.worker.running)
        self.assertIsNotNone(self.children[-1].returncode)
