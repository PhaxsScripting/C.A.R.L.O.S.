import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from ev.commands import clock_request
from ev.ai import LocalHybridProvider
from ev.tools.builtin import get_clock


class ClockTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_failed_requests_use_clock_without_chat_model(self):
        provider = LocalHybridProvider({})
        provider.local.begin = AsyncMock(
            side_effect=AssertionError("Clock requests must not invoke model")
        )
        for text in (
            "What time is it?",
            "give me the time",
            "What the fuck is the time right now?",
            "No, forget about Apple. We're not worrying about Apple anymore. Forget that shit. We're only managing my PC. What time is it right now?",
            "Can you tell me the current time please?",
            "what's today's date?",
        ):
            with self.subTest(text=text):
                turn = await provider.begin(
                    text, [{"role": "assistant", "content": "The current time is now."}], [], []
                )
                self.assertEqual(turn.tool_calls[0].name, "system.clock")
                result = await provider.continue_with_tools(
                    turn,
                    [
                        (
                            turn.tool_calls[0],
                            {
                                "status": "completed",
                                "result": {
                                    "local_time": "1:42 PM",
                                    "local_date": "Saturday, September 05, 2026",
                                    "timezone": "EDT",
                                },
                            },
                        )
                    ],
                    [],
                )
                self.assertTrue("1:42 PM" in result.text or "September 05, 2026" in result.text)
                self.assertNotIn("time is now", result.text)
        provider.local.begin.assert_not_awaited()

    def test_clock_does_not_capture_timers_examples_or_other_timezones(self):
        for text in (
            "What's the timer now?",
            "set a timer for five minutes",
            "How much time is left?",
            "What time is it in Tokyo?",
            'type "what time is it?"',
            "Explain what time is",
            "don't tell me the time",
            "set the time to noon",
        ):
            self.assertIsNone(clock_request(text), text)

    def test_clock_response_reads_os_clock_and_includes_timezone(self):
        result = get_clock({}, None)
        observed = datetime.fromisoformat(result["iso8601"])
        self.assertLess(abs((datetime.now(timezone.utc) - observed).total_seconds()), 2)
        self.assertTrue(result["local_time"])
        self.assertTrue(result["timezone"])

    async def test_missing_clock_evidence_does_not_invent_a_time(self):
        provider = LocalHybridProvider({})
        turn = await provider.begin("What time is it?", [], [], [])
        result = await provider.continue_with_tools(
            turn, [(turn.tool_calls[0], {"status": "failed", "result": {}})], []
        )
        self.assertEqual(result.text, "I couldn't read the system clock.")

    async def test_misheard_timer_checks_actual_timers_before_clock(self):
        provider = LocalHybridProvider({})
        provider.local.begin = AsyncMock(side_effect=AssertionError("No model for timer status"))
        turn = await provider.begin("What's the timer now?", [], [], [])
        self.assertEqual(turn.tool_calls[0].name, "reminders.list")
        clock = await provider.continue_with_tools(
            turn, [(turn.tool_calls[0], {"result": {"reminders": []}})], []
        )
        self.assertEqual(clock.tool_calls[0].name, "system.clock")
        answer = await provider.continue_with_tools(
            clock,
            [
                (
                    clock.tool_calls[0],
                    {
                        "result": {
                            "local_time": "1:42 PM",
                            "local_date": "Saturday",
                            "timezone": "EDT",
                        }
                    },
                )
            ],
            [],
        )
        self.assertEqual(answer.text, "There isn't an active timer. It's 1:42 PM EDT.")
        active = await provider.continue_with_tools(
            turn, [(turn.tool_calls[0], {"result": {"reminders": [{"label": "tea"}]}})], []
        )
        self.assertIn("tea", active.text)
        self.assertFalse(active.tool_calls)
