import asyncio
import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from ev.goals import validate_conditions, verify_conditions
from ev.paths import Paths
from ev.service import CarlosCore
from ev.tools.plans import build_plan
from ev.tools.base import ValidationError
from ev.ai.base import ProviderTurn, ToolCall
from ev.ai.nvidia import NvidiaProvider


class PredicateTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_predicate_hashes_literal_utf8_instead_of_asking_model_for_digest(self):
        for text in ("", "hello", "hello\n", "café 🌙"):
            with self.subTest(text=text):
                request = AsyncMock(
                    return_value={
                        "status": "completed",
                        "result": {"sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()},
                    }
                )
                conditions = [{"kind": "file_text", "path": "/fixture", "expected": text}]
                self.assertTrue(
                    (await verify_conditions(conditions, request, "text-test"))["verified"]
                )
                request.assert_awaited_once_with(
                    {"name": "files.hash", "arguments": {"path": "/fixture"}}, "text-test"
                )
                request.return_value["result"]["sha256"] = hashlib.sha256(
                    (text + "\n").encode("utf-8")
                ).hexdigest()
                self.assertFalse(
                    (await verify_conditions(conditions, request, "text-test"))["verified"]
                )

    async def test_text_predicate_rejects_bad_expected_text_before_observation(self):
        for expected in (None, True, 3, {}, "x" * 65537, "\ud800"):
            with self.subTest(type=type(expected).__name__), self.assertRaises(ValidationError):
                validate_conditions(
                    [{"kind": "file_text", "path": "/fixture", "expected": expected}]
                )

    async def test_semantic_condition_reads_exact_state_not_click_acceptance(self):
        target = {
            "window_id": "window-id",
            "process_id": 12,
            "window_title": "Fixture",
            "path": "/1/2",
            "name": "Option",
            "role": "check box",
            "application": "Fixture",
        }
        condition = [
            {"kind": "control_state", "target": target, "property": "checked", "expected": True}
        ]
        request = AsyncMock(
            return_value={
                "status": "completed",
                "result": {"element": {"states": {"checked": True}}},
            }
        )
        self.assertTrue((await verify_conditions(condition, request, "test"))["verified"])
        self.assertEqual(
            request.await_args.args[0], {"name": "desktop.controls.inspect", "arguments": target}
        )
        request.return_value = {"status": "completed", "result": {"action_accepted": True}}
        self.assertFalse((await verify_conditions(condition, request, "test"))["verified"])
        request.return_value = {
            "status": "completed",
            "result": {"element": {"states": {"checked": False}}},
        }
        self.assertFalse((await verify_conditions(condition, request, "test"))["verified"])

    async def test_semantic_condition_rejects_unbound_or_scalar_wrong_type(self):
        with self.assertRaises(ValidationError):
            validate_conditions(
                [
                    {
                        "kind": "control_state",
                        "target": {"name": "Option"},
                        "property": "checked",
                        "expected": True,
                    }
                ]
            )

    async def test_fresh_window_read_is_shared_and_exact(self):
        request = AsyncMock(
            return_value={
                "status": "completed",
                "result": {
                    "windows": [{"id": "exact", "minimized": True}],
                    "active_window_id": "exact",
                    "captured_at_monotonic": time.monotonic(),
                },
            }
        )
        result = await verify_conditions(
            [
                {"kind": "window_active", "window_id": "exact"},
                {
                    "kind": "window_state",
                    "window_id": "exact",
                    "property": "minimized",
                    "expected": True,
                },
            ],
            request,
            "test",
        )
        self.assertTrue(result["verified"])
        request.assert_awaited_once()

    async def test_missing_false_empty_and_stale_are_not_equivalent(self):
        for data in (
            {},
            {"windows": []},
            {"windows": [], "captured_at_monotonic": time.monotonic() - 5},
            {"windows": [], "windows_truncated": True, "captured_at_monotonic": time.monotonic()},
        ):
            with self.subTest(data=data):
                request = AsyncMock(return_value={"status": "completed", "result": data})
                result = await verify_conditions(
                    [{"kind": "window_absent", "window_id": "exact"}], request, "test"
                )
                self.assertFalse(result["verified"])

    async def test_invalid_and_future_window_timestamps_are_not_fresh_evidence(self):
        for captured in (float("nan"), float("inf"), True, "123", time.monotonic() + 10):
            request = AsyncMock(
                return_value={
                    "status": "completed",
                    "result": {"windows": [], "captured_at_monotonic": captured},
                }
            )
            result = await verify_conditions(
                [{"kind": "window_absent", "window_id": "exact"}], request, "test"
            )
            self.assertFalse(result["verified"], captured)

    async def test_no_model_truth_or_arbitrary_tool_predicates(self):
        for conditions in (
            [],
            [{"kind": "shell", "command": "true"}],
            [{"kind": "audio_muted", "expected": "false"}],
            [{"kind": "window_state", "window_id": "x", "property": "title", "expected": True}],
        ):
            with self.subTest(conditions=conditions), self.assertRaises(ValidationError):
                validate_conditions(conditions)

    async def test_failed_observation_and_cancel_fail_closed(self):
        request = AsyncMock(return_value={"status": "failed", "result": {"muted": True}})
        condition = [{"kind": "audio_muted", "expected": True}]
        self.assertFalse((await verify_conditions(condition, request, "test"))["verified"])
        request.reset_mock()
        self.assertFalse(
            (await verify_conditions(condition, request, "test", cancelled=lambda: True))[
                "verified"
            ]
        )
        request.assert_not_awaited()


class GoalPlanTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = CarlosCore(
            paths=Paths(*(self.root / n for n in ("config", "data", "state", "cache", "runtime")))
        )
        self.service.config["security"]["allowed_roots"] = [str(self.root)]
        self.path = self.root / "fixture.txt"
        self.args = {
            "goal": "Create exact fixture text",
            "steps": [
                {
                    "group": "prepare",
                    "steps": [
                        {
                            "id": "create",
                            "tool": "files.text.create",
                            "arguments": {"path": str(self.path), "content": "fixture"},
                        }
                    ],
                }
            ],
            "conditions": [
                {
                    "kind": "file_hash",
                    "path": {"$ref": "create.result.path"},
                    "sha256": hashlib.sha256(b"fixture").hexdigest(),
                }
            ],
        }

    async def asyncTearDown(self):
        self.service.memory.close()
        self.temp.cleanup()

    async def run_plan(self):
        return await self.service._request_model_tool(
            {"name": "agent.execute_plan", "arguments": self.args}, "test-goal"
        )

    async def test_real_plan_ref_and_fresh_hash_without_deadlock(self):
        result = await asyncio.wait_for(self.run_plan(), 4)
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["result"]["verified"])
        self.assertFalse(result["result"]["goal_verified"])
        self.assertEqual(self.path.read_text(), "fixture")
        self.assertIsNone(self.service.planner.active)

    async def test_numbered_string_step_ids_and_exact_text_condition_execute(self):
        self.args["steps"][0]["steps"][0]["id"] = "1"
        self.args["conditions"] = [
            {"kind": "file_text", "path": {"$ref": "1.result.path"}, "expected": "fixture"}
        ]
        result = await self.run_plan()
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["result"]["verified"])
        self.assertEqual(self.path.read_bytes(), b"fixture")

    async def test_invalid_numbered_step_ids_rejected_before_writes(self):
        for identifier in (1, "", "1.bad", "../1", "1" * 41):
            self.args["steps"][0]["steps"][0]["id"] = identifier
            result = await self.run_plan()
            self.assertEqual(result["status"], "failed", result)
            self.assertEqual(result["result"]["error_code"], "plan_validation_failed")
            self.assertFalse(result["result"]["executed"])
            self.assertFalse(self.path.exists())

    async def test_plan_argument_error_identifies_step_and_fields_without_coercing(self):
        step = self.args["steps"][0]["steps"][0]
        step["arguments"] = {"path": str(self.path), "expected": "fixture"}
        result = await self.run_plan()
        evidence = result["result"]
        self.assertFalse(evidence["executed"])
        for text in (
            "Step create",
            "files.text.create",
            "missing required fields ['content']",
            "unknown fields ['expected']",
        ):
            self.assertIn(text, evidence["error"])
        self.assertFalse(self.path.exists())

    async def test_model_can_repair_schema_error_then_verify_exact_text(self):
        conditions = [{"kind": "file_text", "path": str(self.path), "expected": "fixture"}]
        first = {
            "goal": "Create exact fixture",
            "conditions": conditions,
            "steps": [
                {
                    "id": "1",
                    "tool": "files.text.create",
                    "arguments": {"path": str(self.path), "expected": "fixture"},
                }
            ],
        }
        second = {
            **first,
            "steps": [
                {
                    "id": "1",
                    "tool": "files.text.create",
                    "arguments": {"path": str(self.path), "content": "fixture"},
                }
            ],
        }
        provider = NvidiaProvider({"model": "test"})
        provider.begin = AsyncMock(
            return_value=ProviderTurn(
                "nvidia", "test", "", [ToolCall("first", "agent.execute_plan", first)]
            )
        )

        async def continue_turn(turn, outputs, tools):
            if turn.tool_calls[0].call_id == "first":
                self.assertFalse(self.path.exists())
                self.assertFalse(outputs[0][1]["result"]["executed"])
                self.assertIn("'content'", outputs[0][1]["result"]["error"])
                return ProviderTurn(
                    "nvidia", "test", "", [ToolCall("fixed", "agent.execute_plan", second)]
                )
            return ProviderTurn("nvidia", "test", "Done")

        provider.continue_with_tools = AsyncMock(side_effect=continue_turn)
        self.service.brain.provider = provider
        result = await self.service.brain.submit("Create the specified file")
        self.assertEqual(result["execution_status"], "DECLARED_CONDITIONS_VERIFIED", result)
        self.assertEqual(result["recovered_failures"], 1)
        self.assertEqual(self.path.read_bytes(), b"fixture")

    async def test_successful_tool_does_not_imply_goal_success(self):
        self.args["conditions"][0]["sha256"] = "0" * 64
        result = await self.run_plan()
        self.assertEqual(result["status"], "failed")
        self.assertTrue(self.path.exists())
        self.assertFalse(result["result"]["goal_verification"]["verified"])

    async def test_all_unsafe_steps_rejected_before_any_write(self):
        for name in (
            "system.power",
            "security.firewall.runtime",
            "files.text_replace",
            "agent.execute_plan",
        ):
            with self.subTest(tool=name):
                args = {
                    **self.args,
                    "steps": [*self.args["steps"], {"id": "unsafe", "tool": name, "arguments": {}}],
                }
                result = await self.service._request_model_tool(
                    {"name": "agent.execute_plan", "arguments": args}, "test-goal"
                )
                self.assertEqual(result["status"], "failed")
                self.assertFalse(self.path.exists())

    async def test_duplicate_mutations_and_forward_refs_rejected(self):
        step = self.args["steps"][0]["steps"][0]
        for extra in (
            {**step, "id": "duplicate"},
            {
                "id": "bad",
                "tool": "files.hash",
                "arguments": {"path": {"$ref": "future.result.path"}},
            },
        ):
            with self.subTest(extra=extra):
                args = {**self.args, "steps": [step, extra]}
                with self.assertRaises(ValidationError):
                    build_plan(args, self.service.planner, "test")

    async def test_failed_step_does_not_run_later_action(self):
        self.path.write_text("existing")
        self.args["steps"].append(
            {
                "id": "later",
                "tool": "files.text.create",
                "arguments": {"path": str(self.root / "later"), "content": "later"},
            }
        )
        result = await self.run_plan()
        self.assertEqual(result["status"], "failed")
        self.assertFalse((self.root / "later").exists())

    async def test_timeout_has_no_automatic_replay_and_archives_plan(self):
        self.args["timeout_seconds"] = 1

        async def delay(spec, arguments):
            await asyncio.sleep(3)
            raise AssertionError("Cancelled worker must not finish")

        self.service.tools.execute = AsyncMock(side_effect=delay)
        # Keep the real composite executor; only child tools are delayed.
        original = self.service.tools.get("agent.execute_plan").executor
        result = await original(self.args, self.service.tools.context)
        self.assertFalse(result["ok"])
        self.assertIsNone(self.service.planner.active)
        self.assertEqual(self.service.planner.recent[-1].status, "INTERRUPTED_UNCERTAIN")

    async def test_verifier_is_registered_readonly_and_cannot_execute_other_tools(self):
        self.assertTrue(self.service.tools.get("agent.verify_conditions").read_only)
        result = await self.service._request_model_tool(
            {
                "name": "agent.verify_conditions",
                "arguments": {
                    "conditions": [
                        {"kind": "file_kind", "path": str(self.root), "expected": "directory"}
                    ]
                },
            },
            "verify",
        )
        self.assertEqual(result["status"], "completed", result)

    async def test_model_switches_strategy_and_verifies_same_goal(self):
        conditions = [{"kind": "file_kind", "path": str(self.path), "expected": "file"}]
        first = {
            "goal": "Prepare a fixture file",
            "conditions": conditions,
            "steps": [
                {
                    "id": "copy",
                    "tool": "files.copy",
                    "arguments": {
                        "source": str(self.root / "missing"),
                        "destination": str(self.path),
                    },
                }
            ],
        }
        second = {"goal": first["goal"], "conditions": conditions, "steps": self.args["steps"]}
        provider = NvidiaProvider({"model": "test"})
        provider.begin = AsyncMock(
            return_value=ProviderTurn(
                "nvidia", "test", "", [ToolCall("first", "agent.execute_plan", first)]
            )
        )
        provider.continue_with_tools = AsyncMock(
            side_effect=[
                ProviderTurn(
                    "nvidia", "test", "", [ToolCall("recovery", "agent.execute_plan", second)]
                ),
                ProviderTurn("nvidia", "test", "Done!"),
            ]
        )
        self.service.brain.provider = provider
        result = await self.service._submit_action_clauses(
            "Prepare a fixture file using an available method", "recovery-test"
        )
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["recovered_failures"], 1)
        self.assertEqual(result["execution_status"], "DECLARED_CONDITIONS_VERIFIED")
        self.assertEqual(self.path.read_text(), "fixture")
        task = self.service.task_journal.get("recovery-test")
        self.assertTrue(
            any(s["tool"] == "files.hash" or s["tool"] == "files.info" for s in task["steps"])
        )

    async def test_reference_does_not_hide_invalid_literal_arguments(self):
        self.args["steps"].append(
            {
                "id": "bad",
                "tool": "files.hash",
                "arguments": {"path": {"$ref": "create.result.path"}, "untrusted": True},
            }
        )
        result = await self.run_plan()
        self.assertEqual(result["status"], "failed")
        self.assertFalse(self.path.exists())

    async def test_plan_can_wait_verify_and_continue_without_lock_recursion(self):
        condition = {
            "kind": "file_text",
            "path": {"$ref": "create.result.path"},
            "expected": "fixture",
        }
        self.args["steps"].extend(
            [
                {
                    "id": "wait",
                    "tool": "agent.wait_for",
                    "arguments": {"conditions": [condition], "timeout_seconds": 2},
                },
                {
                    "id": "verify",
                    "tool": "agent.verify_conditions",
                    "arguments": {"conditions": [condition]},
                },
                {
                    "id": "hash",
                    "tool": "files.hash",
                    "arguments": {"path": {"$ref": "create.result.path"}},
                },
            ]
        )
        result = await asyncio.wait_for(self.run_plan(), 4)
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["result"]["verified"])
        self.assertEqual(len(result["result"]["steps"]), 4)
        self.assertIsNone(self.service.planner.active)

    async def test_invalid_wait_predicate_rejected_before_any_plan_effect(self):
        for condition in (
            {"kind": "shell", "command": "true"},
            {"kind": "window_exists", "window_id": {"$ref": "future.result.id"}},
            {"kind": "window_exists", "window_id": {"fake": "id"}},
            {"kind": "file_text", "path": str(self.path), "expected": 10},
        ):
            self.args["steps"] = self.args["steps"][:1] + [
                {"id": "wait", "tool": "agent.wait_for", "arguments": {"conditions": [condition]}}
            ]
            result = await self.run_plan()
            self.assertEqual(result["result"]["error_code"], "plan_validation_failed", result)
            self.assertFalse(self.path.exists())

    async def test_unsatisfied_wait_stops_plan_without_later_write_or_replay(self):
        self.args["steps"].insert(
            0,
            {
                "id": "wait",
                "tool": "agent.wait_for",
                "arguments": {
                    "conditions": [
                        {"kind": "file_kind", "path": str(self.path), "expected": "file"}
                    ],
                    "timeout_seconds": 1,
                    "interval_seconds": 0.5,
                },
            },
        )
        result = await asyncio.wait_for(self.run_plan(), 3)
        self.assertEqual(result["status"], "failed", result)
        self.assertFalse(self.path.exists())
        self.assertEqual(result["result"]["steps"][0]["attempts"], 1)

    async def test_cancel_during_composed_wait_never_runs_later_steps(self):
        entered = asyncio.Event()
        from ev.task_wait import wait_for_conditions

        async def observed_wait(*args, **kwargs):
            entered.set()
            return await wait_for_conditions(*args, **kwargs)

        self.args["steps"].insert(
            0,
            {
                "id": "wait",
                "tool": "agent.wait_for",
                "arguments": {
                    "conditions": [
                        {"kind": "file_kind", "path": str(self.path), "expected": "file"}
                    ],
                    "timeout_seconds": 30,
                },
            },
        )
        with patch("ev.tools.waiting.wait_for_conditions", side_effect=observed_wait):
            running = asyncio.create_task(self.run_plan())
            await asyncio.wait_for(entered.wait(), 2)
            running.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await running
        self.assertFalse(self.path.exists())
        self.assertIsNone(self.service.planner.active)

    def media_reference_plan(self):
        condition = {
            "kind": "media_playback",
            "service": {"$ref": "observe.result.players.0.service"},
            "owner": {"$ref": "observe.result.players.0.owner"},
            "expected": "Paused",
        }
        return {
            "goal": "Observe the same paused media player",
            "steps": [
                {
                    "id": "observe",
                    "tool": "audio.players",
                    "arguments": {"service": "org.mpris.MediaPlayer2.fixture"},
                },
                {
                    "id": "verify",
                    "tool": "agent.verify_conditions",
                    "arguments": {"conditions": [condition]},
                },
                {
                    "id": "create",
                    "tool": "files.text.create",
                    "arguments": {"path": str(self.path), "content": "fixture"},
                },
            ],
            "conditions": [condition],
        }

    async def test_control_target_references_pass_exact_identity_to_verification(self):
        target = {
            "window_id": "exact",
            "process_id": 123,
            "window_title": "Fixture",
            "application": "Fixture",
            "name": "Search",
            "role": "entry",
            "path": "/exact",
        }
        for referenced in (
            {"$ref": "resolve.result.target"},
            {key: {"$ref": "resolve.result.target." + key} for key in target},
        ):
            condition = {"kind": "control_text", "target": referenced, "expected": "requested"}
            self.args = {
                "goal": "Verify exact requested field text",
                "steps": [
                    {
                        "id": "resolve",
                        "tool": "desktop.controls.resolve",
                        "arguments": {"window_id": "exact", "name": "Search", "role": "entry"},
                    },
                    {
                        "id": "verify",
                        "tool": "agent.verify_conditions",
                        "arguments": {"conditions": [condition]},
                    },
                ],
                "conditions": [condition],
            }

            async def inspect(arguments, context):
                self.assertEqual(arguments, target)
                return {"text_available": True, "text": "requested", "text_truncated": False}

            with patch.object(
                self.service.tools.get("desktop.controls.resolve"),
                "executor",
                AsyncMock(return_value={"target": target, "resolved": True}),
            ), patch.object(
                self.service.tools.get("desktop.controls.inspect"), "executor", side_effect=inspect
            ):
                result = await self.run_plan()
            self.assertEqual(result["status"], "completed", result)
            self.assertTrue(result["result"]["verified"])

    async def test_control_reference_preflight_rejects_unknown_identity_or_self_matching_value(
        self,
    ):
        for change in (
            {"target": {"$ref": "future.result.target"}},
            {"target": {"$ref": "create.result.target", "name": "extra"}},
            {"target": {"name": {"$ref": "create.result.name"}}},
            {"expected": {"$ref": "create.result.text"}},
        ):
            self.args["conditions"] = [
                {
                    "kind": "control_text",
                    "target": {"$ref": "create.result.target"},
                    "expected": "requested",
                    **change,
                }
            ]
            result = await self.run_plan()
            self.assertEqual(result["result"]["error_code"], "plan_validation_failed", result)
            self.assertFalse(self.path.exists())

    async def test_resolved_invalid_control_identity_rejected_before_inspection(self):
        condition = {
            "kind": "control_text",
            "target": {"$ref": "resolve.result.target"},
            "expected": "requested",
        }
        self.args = {
            "goal": "Verify field",
            "steps": [
                {
                    "id": "resolve",
                    "tool": "desktop.controls.resolve",
                    "arguments": {"window_id": "exact", "name": "Search", "role": "entry"},
                },
                {
                    "id": "verify",
                    "tool": "agent.verify_conditions",
                    "arguments": {"conditions": [condition]},
                },
                self.args["steps"][0],
            ],
            "conditions": [condition],
        }
        with patch.object(
            self.service.tools.get("desktop.controls.resolve"),
            "executor",
            AsyncMock(return_value={"target": {"name": "Search"}, "resolved": True}),
        ), patch.object(
            self.service.tools.get("desktop.controls.inspect"), "executor", AsyncMock()
        ) as inspect:
            result = await self.run_plan()
        self.assertEqual(result["status"], "failed", result)
        inspect.assert_not_awaited()
        self.assertFalse(self.path.exists())

    async def test_media_identity_references_resolve_in_steps_and_final_conditions(self):
        self.args = self.media_reference_plan()

        async def observed(*args):
            return {
                "players": [
                    {
                        "service": "org.mpris.MediaPlayer2.fixture",
                        "owner": ":1.23",
                        "playback_status": "Paused",
                        "captured_at_monotonic": time.monotonic(),
                    }
                ],
                "partial": False,
            }

        with patch.object(
            self.service.tools.get("audio.players"), "executor", side_effect=observed
        ):
            result = await self.run_plan()
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["result"]["verified"])
        self.assertEqual(self.path.read_text(), "fixture")

    async def test_media_references_do_not_allow_expected_state_or_forward_identity(self):
        for field, value in (
            ("expected", {"$ref": "observe.result.players.0.playback_status"}),
            ("owner", {"$ref": "future.result.owner"}),
            ("service", {"fake": "spotify"}),
        ):
            self.args = self.media_reference_plan()
            self.args["conditions"][0][field] = value
            result = await self.run_plan()
            self.assertEqual(result["result"]["error_code"], "plan_validation_failed", result)
            self.assertFalse(self.path.exists())

    async def test_changed_or_invalid_resolved_media_owner_stops_before_later_effect(self):
        for owner in (":1.24", "invalid"):
            self.args = self.media_reference_plan()
            count = 0

            async def observed(*args):
                nonlocal count
                count += 1
                return {
                    "players": [
                        {
                            "service": "org.mpris.MediaPlayer2.fixture",
                            "owner": (
                                "invalid"
                                if owner == "invalid"
                                else ":1.23" if count == 1 else owner
                            ),
                            "playback_status": "Paused",
                            "captured_at_monotonic": time.monotonic(),
                        }
                    ],
                    "partial": False,
                }

            with patch.object(
                self.service.tools.get("audio.players"), "executor", side_effect=observed
            ):
                result = await self.run_plan()
            self.assertEqual(result["status"], "failed", result)
            self.assertFalse(self.path.exists())
