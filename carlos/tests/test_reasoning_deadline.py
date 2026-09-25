import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from ev.ai.base import ProviderTurn, ToolCall
from ev.brain import PendingCommand, ReasoningBudgetExceeded
from ev.paths import Paths
from ev.service import CarlosCore
from ev.state import CoreState


class ReasoningDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.service = CarlosCore(
            paths=Paths(*(root / n for n in ("config", "data", "state", "cache", "runtime")))
        )
        self.addCleanup(self.service.memory.close)
        self.brain = self.service.brain
        self.brain.task_timeout_seconds = 0.15

    async def test_stalled_initial_reasoning_cancelled_without_offline_lockout(self):
        cancelled = asyncio.Event()

        async def stalled(*args):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()

        self.brain.provider.begin = AsyncMock(side_effect=stalled)
        self.brain.request_tool = AsyncMock()
        result = await asyncio.wait_for(self.brain.submit("Tell me something", "deadline"), 1)
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("Reasoning time budget", result["response"])
        self.assertTrue(cancelled.is_set())
        self.brain.request_tool.assert_not_awaited()
        self.assertEqual(self.service.state.current, CoreState.DORMANT)
        self.assertNotIn("deadline", self.brain._tool_history)

    async def test_expired_continuation_retains_receipts_and_does_not_repeat_action(self):
        self.brain.provider.begin = AsyncMock(
            return_value=ProviderTurn("test", "test", "", [ToolCall("a", "fixture.action", {})])
        )
        self.brain.request_tool = AsyncMock(
            return_value={
                "status": "completed",
                "result": {"verified": True},
                "execution": {"verified": True, "scope": "tool_effect"},
            }
        )

        async def stalled(*args):
            await asyncio.sleep(10)

        self.brain.provider.continue_with_tools = AsyncMock(side_effect=stalled)
        result = await asyncio.wait_for(self.brain.submit("Run fixture", "continuation"), 1)
        self.assertEqual(result["status"], "failed", result)
        self.assertFalse(result["goal_verified"])
        self.assertEqual(result["tool_receipts"][0]["tool"], "fixture.action")
        self.brain.request_tool.assert_awaited_once()
        self.assertEqual(self.service.state.current, CoreState.DORMANT)

    async def test_expired_permission_continuation_never_starts_another_request(self):
        call = ToolCall("confirmed", "fixture.action", {})
        turn = ProviderTurn("test", "test", "", [call])
        self.brain.pending["confirmation"] = PendingCommand(
            "confirmed-task", turn, [], call, [], time.monotonic() - 10
        )
        self.brain.provider.continue_with_tools = AsyncMock()
        result = await self.brain.resume_confirmation(
            "confirmation", {"status": "completed", "execution": {"verified": True}}
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(result["tool_receipts"]), 1)
        self.brain.provider.continue_with_tools.assert_not_awaited()
        self.assertNotIn("confirmation", self.brain.pending)
        self.assertEqual(self.service.state.current, CoreState.DORMANT)

    async def test_observation_wait_credit_and_wall_cap_both_bound_provider(self):
        self.brain.task_timeout_seconds = 120
        self.brain._wait_seconds["task"] = 300
        with patch("ev.brain.time.monotonic", return_value=400):
            self.assertEqual(self.brain._reasoning_remaining("task", 0), 20)
            self.assertLess(self.brain._reasoning_remaining("other", 0), 0)
        self.brain.task_timeout_seconds = 10000
        with patch("ev.brain.time.monotonic", return_value=3601):
            request = AsyncMock()
            with self.assertRaises(ReasoningBudgetExceeded):
                await self.brain._provider_turn(request, "task", 0)
            request.assert_not_awaited()

    async def test_external_cancellation_is_not_relabelled_as_deadline(self):
        request = AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await self.brain._provider_turn(request, "task", time.monotonic())
