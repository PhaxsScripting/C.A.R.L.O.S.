import logging
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from ev.ai.base import ProviderTurn, ToolCall
from ev.events import PhaxEventBus
from ev.execution import ExecutionController
from ev.paths import Paths
from ev.planner import PlanStep, TaskPlanner
from ev.permissions import Permission
from ev.service import CarlosCore
from ev.tools.base import ToolContext, ToolRegistry, ToolSpec, ValidationError, validate_schema
from ev.tools.results import evaluate_result


class ResultContractTests(unittest.TestCase):
    def test_unknown_tool_never_inherits_verification_from_prose_or_ok(self):
        for payload in ({}, {"ok": True}, {"message": "Everything is done"}):
            result = evaluate_result("plugin.new_action", payload)
            self.assertTrue(result.ok)
            self.assertFalse(result.verified)
            self.assertEqual(result.status, "EXECUTED_UNVERIFIED")

    def test_failed_and_truncated_payloads_fail_even_if_verified_is_true(self):
        for name in ("audio.set_volume", "applications.open", "system.identity", "plugin.action"):
            for failure in ({"ok": False}, {"error": "backend failed"}, {"truncated": True}):
                with self.subTest(name=name, failure=failure):
                    result = evaluate_result(name, {"verified": True, **failure})
                    self.assertFalse(result.ok)
                    self.assertEqual(result.status, "FAILED")

    def test_dispatch_and_input_acceptance_are_not_postconditions(self):
        for name, flag in {
            "applications.open": "launched",
            "applications.focus": "launched",
            "browser.open_url": "requested",
            "settings.open": "requested",
            "desktop.keyboard.type_text": "input_sent",
            "desktop.pointer.click": "input_sent",
            "browser.shortcut": "input_sent",
            "accessibility.element.activate": "action_accepted",
        }.items():
            with self.subTest(name=name):
                result = evaluate_result(name, {flag: True, "verified": True})
                self.assertTrue(result.ok)
                self.assertFalse(result.verified)
                self.assertEqual(result.status, "EXECUTED_UNVERIFIED")

    def test_read_only_is_explicit_not_permission_inference(self):
        result = evaluate_result("system.identity", {"hostname": "test"})
        self.assertEqual(result.scope, "observation_only")
        self.assertFalse(result.changed_state)
        self.assertTrue(evaluate_result("plugin.observe", {"value": 1}, read_only=True).verified)
        self.assertFalse(evaluate_result("plugin.observe", {"value": 1}).verified)

    def test_postcondition_failure_and_power_scheduling(self):
        self.assertFalse(evaluate_result("desktop.window.state", {"verified": False}).ok)
        self.assertTrue(evaluate_result("desktop.window.state", {"verified": True}).verified)
        self.assertFalse(
            evaluate_result("system.power", {"verified": True, "scheduled": True}).verified
        )

    def test_schema_min_items_is_enforced(self):
        with self.assertRaises(ValidationError):
            validate_schema([], {"type": "array", "minItems": 1})

    def test_unknown_safe_mutation_does_not_gain_automatic_replay(self):
        self.assertFalse(
            TaskPlanner._retryable(PlanStep("one", "plugin.action", {}, permission_class="SAFE"))
        )
        self.assertTrue(
            TaskPlanner._retryable(PlanStep("one", "system.identity", {}, permission_class="SAFE"))
        )


class ExecutionReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.service = CarlosCore(
            paths=Paths(*(root / n for n in ("config", "data", "state", "cache", "runtime")))
        )
        self.service.tools.execute = AsyncMock(side_effect=AssertionError("No live actions"))

    async def asyncTearDown(self):
        self.service.memory.close()
        self.temp.cleanup()

    async def test_direct_ipc_gets_failure_contract_not_completed(self):
        self.service.tools.execute.side_effect = None
        self.service.tools.execute.return_value = {"ok": False, "error": "No backend"}
        result = await self.service.request_tool(
            {"name": "audio.media", "arguments": {"action": "pause"}}, "failure"
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["execution"]["status"], "FAILED")

    async def test_new_tool_receives_shared_execution_contract(self):
        registry = ToolRegistry(
            ToolContext(
                {"security": {"max_tool_output_bytes": 4096}},
                PhaxEventBus(),
                logging.getLogger("test"),
            )
        )
        spec = ToolSpec(
            "test.action",
            "TEST",
            "Test",
            Permission.SAFE,
            {"type": "object"},
            lambda a, c: {"ok": True},
        )
        registry.register(spec)
        payload, result = await ExecutionController(registry).execute(spec, {})
        self.assertTrue(payload["ok"])
        self.assertEqual(result.status, "EXECUTED_UNVERIFIED")

    async def test_false_model_completion_is_replaced_by_tool_receipt(self):
        brain = self.service.brain
        brain._tool_history["receipt"] = [
            (
                ToolCall("one", "applications.open", {}),
                {"status": "completed", "execution": {"verified": False}},
            )
        ]
        result = await brain._complete(
            ProviderTurn("test", "test", "I opened everything and fixed your project."),
            "receipt",
            time.monotonic(),
        )
        self.assertNotIn("fixed your project", result["response"])
        self.assertEqual(result["execution_status"], "EXECUTED_UNVERIFIED")
        self.assertFalse(result["goal_verified"])

    async def test_model_cannot_erase_failed_tool_with_success_text(self):
        brain = self.service.brain
        brain._tool_history["failure"] = [
            (ToolCall("one", "audio.media", {}), {"status": "failed"})
        ]
        result = await brain._complete(
            ProviderTurn("test", "test", "Done!"), "failure", time.monotonic()
        )
        self.assertEqual(result["status"], "failed")

    async def test_no_tool_action_claim_does_not_become_success(self):
        result = await self.service.brain._complete(
            ProviderTurn("test", "test", "I've opened Firefox."), "empty", time.monotonic()
        )
        self.assertEqual(result["execution_status"], "BLOCKED")

    async def test_casual_promises_without_calls_are_blocked(self):
        for text in ("Okay, I'll unpause your Spotify music.", "Opening YouTube in Firefox..."):
            result = await self.service.brain._complete(
                ProviderTurn("test", "test", text), "promise", time.monotonic()
            )
            self.assertEqual(result["execution_status"], "BLOCKED")
            self.assertEqual(result["status"], "failed")

    async def test_invalid_model_decision_retains_prior_receipts_without_offline_state(self):
        from ev.ai.base import ProviderDecisionError

        brain = self.service.brain
        brain.provider.begin = AsyncMock(
            return_value=ProviderTurn(
                "test", "test", "", [ToolCall("one", "audio.media", {"action": "play"})]
            )
        )
        brain.provider.continue_with_tools = AsyncMock(
            side_effect=ProviderDecisionError("Unknown tool")
        )
        brain.request_tool = AsyncMock(
            return_value={"status": "completed", "execution": {"verified": True}}
        )
        result = await brain.submit("Fixture request", "decision")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["tool_receipts"][0]["tool"], "audio.media")
        self.assertNotIn("unavailable", result["response"])
        self.assertFalse(
            any(
                e["type"] == "core.state_changed" and e["payload"].get("to") == "OFFLINE"
                for e in self.service.bus.history()
            )
        )

    async def test_transport_error_after_action_keeps_receipts_and_does_not_replay(self):
        from ev.ai.base import ProviderError

        brain = self.service.brain
        brain.provider.begin = AsyncMock(
            return_value=ProviderTurn(
                "test", "test", "", [ToolCall("one", "audio.media", {"action": "play"})]
            )
        )
        brain.provider.continue_with_tools = AsyncMock(side_effect=ProviderError("Connection lost"))
        brain.request_tool = AsyncMock(
            return_value={"status": "completed", "execution": {"verified": True}}
        )
        result = await brain.submit("Fixture request", "connection-loss")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["tool_receipts"][0]["tool"], "audio.media")
        self.assertEqual(brain.request_tool.await_count, 1)

    async def test_normal_conversation_remains_intact(self):
        text = "Recursion is a function calling itself."
        result = await self.service.brain._complete(
            ProviderTurn("test", "test", text), "chat", time.monotonic()
        )
        self.assertEqual(result["response"], text)
        self.assertEqual(result["execution_status"], "ANSWERED")

    async def test_read_only_lookup_cannot_justify_claiming_an_app_was_opened(self):
        self.service.brain._tool_history["lookup"] = [
            (
                ToolCall("one", "applications.list", {}),
                {"status": "completed", "result": {"applications": []}},
            )
        ]
        result = await self.service.brain._complete(
            ProviderTurn("test", "test", "I opened Firefox."), "lookup", time.monotonic()
        )
        self.assertEqual(result["execution_status"], "BLOCKED")

    async def test_repeated_identical_model_calls_stop_before_fourth_execution(self):
        brain = self.service.brain
        turn = ProviderTurn("test", "test", "", [ToolCall("one", "system.identity", {})])
        brain._tool_history["loop"] = [
            (turn.tool_calls[0], {"status": "completed", "result": {"hostname": "test"}})
        ] * 3
        brain.request_tool = AsyncMock(side_effect=AssertionError("No fourth replay"))
        result = await brain._advance(turn, [], "loop", time.monotonic())
        self.assertEqual(result["status"], "failed")
        brain.request_tool.assert_not_awaited()

    async def test_more_than_four_rounds_can_complete_with_fresh_evidence(self):
        brain = self.service.brain
        first = ProviderTurn("test", "test", "", [ToolCall("one", "system.identity", {})])
        brain.request_tool = AsyncMock(
            return_value={"status": "completed", "result": {"hostname": "test"}}
        )
        brain.provider.continue_with_tools = AsyncMock(
            return_value=ProviderTurn("test", "test", "Observed.")
        )
        result = await brain._advance(first, [], "longer", time.monotonic(), depth=6)
        self.assertEqual(result["status"], "completed")
        brain.request_tool.assert_awaited_once()
