"""Routing integration tests: real service/planner, no live desktop mutations."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from ev.ai.base import ProviderTurn, ToolCall
from ev.ai.nvidia import NvidiaProvider
from ev.paths import Paths
from ev.service import CarlosCore
from ev.state import CoreState


class NvidiaPlannerRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.service = CarlosCore(
            paths=Paths(*(root / n for n in ("config", "data", "state", "cache", "runtime")))
        )
        self.service.brain.provider = NvidiaProvider({"model": "test"})
        self.service.brain.provider._request = AsyncMock(
            side_effect=AssertionError("Unexpected network request")
        )
        self.service.tools.execute = AsyncMock(side_effect=AssertionError("Unexpected OS action"))

    async def asyncTearDown(self):
        self.service.memory.close()
        self.temp.cleanup()

    async def test_open_firefox_resolves_exact_desktop_id_without_cloud(self):
        self.service.tools.execute.side_effect = [
            {"applications": [{"desktop_id": "firefox", "name": "Firefox", "match_score": 100}]},
            {"launched": True, "desktop_id": "firefox", "verification": "launcher_acceptance_only"},
        ]
        result = await self.service._submit_action_clauses("open firefox", "open-test")
        self.assertEqual(result["status"], "completed")
        calls = self.service.tools.execute.await_args_list
        self.assertEqual(
            [c.args[0].name for c in calls], ["applications.list", "applications.open"]
        )
        self.assertEqual(calls[1].args[1], {"desktop_id": "firefox"})
        self.assertFalse(result["plan"]["steps"][-1]["verification"]["verified"])
        self.assertIn("not been verified", result["response"])
        self.service.brain.provider._request.assert_not_awaited()
        self.assertEqual(self.service.state.current, CoreState.DORMANT)

    async def test_ambiguous_open_never_launches(self):
        self.service.tools.execute.side_effect = None
        self.service.tools.execute.return_value = {
            "applications": [
                {"desktop_id": "one", "match_score": 90},
                {"desktop_id": "two", "match_score": 90},
            ]
        }
        result = await self.service._submit_action_clauses("open firefox")
        self.assertEqual(result["status"], "failed")
        self.assertTrue(
            all(
                c.args[0].name == "applications.list"
                for c in self.service.tools.execute.await_args_list
            )
        )

    async def test_basic_existing_plans_work_with_nvidia_active(self):
        examples = [
            ("minimize my window", ["desktop.window.resolve", "desktop.window.state"]),
            ("set volume to 30 percent", ["audio.set_volume"]),
            ("pause Spotify", ["audio.media"]),
            ("open https://example.com", ["browser.open_url"]),
        ]

        async def execute(spec, args):
            if spec.name == "desktop.window.resolve":
                return {"resolved": True, "window": {"id": "test-window"}}
            if spec.name == "browser.open_url":
                return {"requested": True, "url": args["url"]}
            return {"verified": True}

        self.service.tools.execute.side_effect = execute
        for phrase, expected in examples:
            with self.subTest(phrase=phrase):
                self.service.tools.execute.reset_mock()
                result = await self.service._submit_action_clauses(phrase)
                self.assertEqual(result["status"], "completed")
                self.assertEqual(
                    [c.args[0].name for c in self.service.tools.execute.await_args_list], expected
                )
        self.service.brain.provider._request.assert_not_awaited()

    async def test_real_nvidia_adapter_calls_planner_and_receives_actual_result(self):
        def reply(content=None, name=None, arguments=None, ident="call1"):
            message = {"content": content}
            if name:
                message["tool_calls"] = [
                    {
                        "id": ident,
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments)},
                    }
                ]
            return {
                "choices": [{"finish_reason": "tool_calls" if name else "stop", "message": message}]
            }

        provider = self.service.brain.provider
        provider._request = AsyncMock(
            side_effect=[
                reply(name="ev__load_tools", arguments={"names": ["system.identity"]}),
                reply(name="system__identity", arguments={}, ident="read-identity"),
                reply(content="Identity inspected."),
            ]
        )
        self.service.tools.execute.side_effect = None
        self.service.tools.execute.return_value = {"hostname": "test-machine"}
        result = await self.service._submit_action_clauses(
            "Investigate this machine for my workflow", "model-test"
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.service.planner.recent), 1)
        plan = self.service.planner.recent[0]
        self.assertEqual(plan.steps[0].tool, "system.identity")
        self.assertEqual(plan.status, "SUCCEEDED")
        messages = provider._request.await_args.args[0]["messages"]
        actual = json.loads(next(m["content"] for m in reversed(messages) if m["role"] == "tool"))
        self.assertEqual(actual["result"]["hostname"], "test-machine")
        self.assertEqual(actual["plan_id"], plan.id)
        self.assertTrue(actual["verification"]["verified"])

    async def test_model_observation_retries_and_updates_entity_history(self):
        self.service.tools.execute.side_effect = [
            ConnectionError("temporary lookup failure"),
            {"resolved": True, "window": {"id": "exact-test-window"}},
        ]
        result = await self.service._request_model_tool(
            {"name": "desktop.window.resolve", "arguments": {"description": "Firefox"}},
            "retry-test",
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.service.tools.execute.await_count, 2)
        self.assertEqual(self.service.planner.last_entities["window"]["id"], "exact-test-window")
        self.assertEqual(self.service.state.current, CoreState.THINKING)

    async def test_model_failed_payload_is_not_verified_success(self):
        self.service.tools.execute.side_effect = None
        self.service.tools.execute.return_value = {"ok": False, "error": "device unavailable"}
        result = await self.service._request_model_tool(
            {"name": "audio.media", "arguments": {"action": "pause"}}, "fail-test"
        )
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["verification"]["verified"])
        self.service.tools.execute.assert_awaited_once()

    async def test_model_does_not_gain_trusted_power_or_admin_authority(self):
        for name, args in [
            ("system.power", {"action": "shutdown"}),
            ("security.firewall.runtime", {"authorize": True}),
        ]:
            with self.subTest(name=name):
                result = await self.service._request_model_tool(
                    {"name": name, "arguments": args}, "authority-test"
                )
                self.assertEqual(result["status"], "failed")
        self.service.tools.execute.assert_not_awaited()

    async def test_cancelled_model_plan_stops_remaining_batch(self):
        provider = self.service.brain.provider
        provider.begin = AsyncMock(
            return_value=ProviderTurn(
                "nvidia",
                "test",
                "",
                [
                    ToolCall("one", "system.identity", {}),
                    ToolCall("two", "system.clock", {}),
                ],
            )
        )
        provider.continue_with_tools = AsyncMock(
            side_effect=AssertionError("Must not continue cancelled task")
        )

        async def execute(spec, args):
            self.service.planner.cancel("user_request")
            return {"hostname": "test-machine"}

        self.service.tools.execute.side_effect = execute
        result = await self.service.brain.submit("Investigate this machine")
        self.assertEqual(result["status"], "cancelled")
        self.service.tools.execute.assert_awaited_once()
        provider.continue_with_tools.assert_not_awaited()

    async def test_confirmation_still_belongs_to_brain_not_two_continuations(self):
        spec = self.service.tools.get("system.identity")
        spec.requires_confirmation = True  # Harmless stand-in for a coding task.
        provider = self.service.brain.provider
        provider.begin = AsyncMock(
            return_value=ProviderTurn("nvidia", "test", "", [ToolCall("one", spec.name, {})])
        )
        provider.continue_with_tools = AsyncMock(
            return_value=ProviderTurn("nvidia", "test", "Identity inspected.")
        )
        self.service.tools.execute.side_effect = None
        self.service.tools.execute.return_value = {"hostname": "test-machine"}
        waiting = await self.service.brain.submit("Investigate this machine")
        self.assertEqual(waiting["status"], "confirmation_required")
        self.assertFalse(self.service.planner.pending)
        self.assertEqual(len(self.service.brain.pending), 1)
        self.service.tools.execute.assert_not_awaited()
        confirmation = waiting["confirmation"]
        result = await self.service.resolve_confirmation(
            {
                "id": confirmation["id"],
                "approval_token": confirmation["approval_token"],
                "approved": True,
            }
        )
        self.assertEqual(result["command"]["status"], "completed")
        self.service.tools.execute.assert_awaited_once()
        provider.continue_with_tools.assert_awaited_once()
        self.assertFalse(self.service.planner.external_pending)
        self.assertEqual(self.service.planner.recent[-1].status, "SUCCEEDED")
        self.assertTrue(
            provider.continue_with_tools.await_args.args[1][0][1]["verification"]["verified"]
        )

    async def test_unknown_model_tool_fails_without_execution(self):
        result = await self.service._request_model_tool(
            {"name": "invented.tool", "arguments": {}}, "invalid-test"
        )
        self.assertEqual(result["status"], "failed")
        self.service.tools.execute.assert_not_awaited()

    async def test_clock_question_uses_no_cloud_round_trip(self):
        self.service.tools.execute.side_effect = None
        self.service.tools.execute.return_value = {
            "local_time": "10:42 PM",
            "local_date": "Sunday, September 06, 2026",
            "timezone": "EDT",
        }
        for phrase, expected in (
            ("what time is it", "10:42 PM"),
            ("what's today's date", "September 06"),
        ):
            result = await self.service._submit_action_clauses(phrase)
            self.assertEqual(result["status"], "completed")
            self.assertIn(expected, result["response"])
        self.service.brain.provider._request.assert_not_awaited()
