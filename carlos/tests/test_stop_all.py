import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from ev.events import PhaxEventBus
from ev.service import CarlosCore


class StopAllTests(unittest.IsolatedAsyncioTestCase):
    def test_only_explicit_global_stop_matches(self):
        for text in ("stop everything", "Cancel all tasks.", "please abort all commands"):
            self.assertTrue(CarlosCore._is_stop_all(text))
        for text in (
            "don't stop everything",
            "explain how to stop everything",
            "stop Spotify",
            "tell me why cancel all exists",
        ):
            self.assertFalse(CarlosCore._is_stop_all(text))

    async def test_global_stop_cancels_queues_power_and_voice_without_running_commands(self):
        service = CarlosCore.__new__(CarlosCore)
        service._action_generation = 2
        service._interactive_task = None
        service._cancel_active_plan = AsyncMock(return_value={"status": "cancelled"})
        service._interrupt_reasoning_for_wake = AsyncMock(return_value=True)
        service.power = SimpleNamespace(cancel=AsyncMock(return_value={"cancelled": True}))
        service.desktop = SimpleNamespace(
            input=SimpleNamespace(cancel_current=MagicMock(), close=AsyncMock())
        )
        service.voice = SimpleNamespace(end_conversation=AsyncMock())
        service.coding_agent = SimpleNamespace(
            cancel_all=MagicMock(return_value={"requested": ["job"]})
        )
        service.bus = PhaxEventBus()
        result = await service._stop_all_actions("stop-test")
        self.assertEqual(service._action_generation, 3)
        service.power.cancel.assert_awaited_once()
        service.coding_agent.cancel_all.assert_called_once()
        service.voice.end_conversation.assert_awaited_once_with("stop_everything", "stop-test")
        self.assertEqual(result["status"], "conversation_ended")
        self.assertIn("may still finish", result["response"])

    async def test_stop_between_compound_steps_prevents_next_action(self):
        service = CarlosCore.__new__(CarlosCore)
        service._action_generation = 0
        service.daily = SimpleNamespace(records=lambda kind: {})
        service.planner = SimpleNamespace(try_plan=lambda *a: None)

        async def first_action(text, correlation):
            service._action_generation += 1
            return {"status": "completed", "response": "First operation completed"}

        service.brain = SimpleNamespace(submit=AsyncMock(side_effect=first_action))
        result = await service._submit_action_clauses_impl(
            "open Firefox and close Spotify", "stop-test"
        )
        self.assertEqual(result["status"], "cancelled")
        service.brain.submit.assert_awaited_once()

    async def test_stop_while_loading_routines_prevents_any_plan(self):
        service = CarlosCore.__new__(CarlosCore)
        service._action_generation = 0

        def stop_during_load(kind):
            service._action_generation += 1
            return {}

        service.daily = SimpleNamespace(records=stop_during_load)
        service.planner = SimpleNamespace(try_plan=MagicMock())
        result = await service._submit_action_clauses_impl("open Firefox", "stop-test")
        self.assertEqual(result["status"], "cancelled")
        service.planner.try_plan.assert_not_called()

    async def test_stop_while_saving_request_prevents_plan_execution(self):
        service = CarlosCore.__new__(CarlosCore)
        service._action_generation = 0
        service.daily = SimpleNamespace(records=lambda kind: {})
        service.planner = SimpleNamespace(
            try_plan=lambda *a: SimpleNamespace(timings={}), execute=AsyncMock()
        )

        def stop_during_save(*args):
            service._action_generation += 1

        service.memory = SimpleNamespace(add_conversation=stop_during_save)
        result = await service._submit_action_clauses_impl("open Firefox", "stop-test")
        self.assertEqual(result["status"], "cancelled")
        service.planner.execute.assert_not_awaited()
