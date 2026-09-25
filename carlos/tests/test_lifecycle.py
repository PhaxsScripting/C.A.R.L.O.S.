import asyncio
import unittest
from unittest.mock import Mock, AsyncMock
from ev.lifecycle import shutdown_step, ShutdownBlocked


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_and_failure_are_observable(self):
        log = Mock()
        self.assertTrue(await shutdown_step("good", AsyncMock(), log))
        self.assertFalse(
            await shutdown_step("bad", AsyncMock(side_effect=ValueError("private data")), log)
        )
        self.assertNotIn("private data", str(log.mock_calls))
        self.assertIn("ValueError", str(log.mock_calls))

    async def test_stuck_cooperative_cleanup_is_cancelled(self):
        cancelled = asyncio.Event()

        async def stuck():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.assertFalse(await shutdown_step("stuck", stuck, Mock(), timeout=0.01))
        self.assertTrue(cancelled.is_set())
        self.assertTrue(await shutdown_step("next", AsyncMock(), Mock()))

    async def test_cancellation_resistant_cleanup_is_not_reported_complete(self):
        release = asyncio.Event()

        async def resistant():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()

        try:
            with self.assertRaises(ShutdownBlocked):
                await shutdown_step("resistant", resistant, Mock(), timeout=0.01, cancel_grace=0.01)
        finally:
            release.set()
            await asyncio.sleep(0)

    async def test_parent_cancellation_propagates(self):
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def operation():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        task = asyncio.create_task(shutdown_step("operation", operation, Mock()))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())
