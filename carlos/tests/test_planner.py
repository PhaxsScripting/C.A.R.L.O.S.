from __future__ import annotations

import sys
import unittest
import asyncio
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "core"))

from ev.events import PhaxEventBus  # noqa: E402
from ev.permissions import Permission  # noqa: E402
from ev.planner import TaskPlanner  # noqa: E402
from ev.state import StateMachine  # noqa: E402
from ev.tools import ToolContext, ToolRegistry, ToolSpec  # noqa: E402


class PlannerTests(unittest.TestCase):
    def test_incidental_actions_in_gui_context_do_not_become_window_fast_paths(self):
        requests = [
            "In the already open disposable application, choose a file in the current folder. Do not modify or launch any other application.",
            "In the editor, explain how the words move Firefox to my other monitor should be parsed",
            "The button says center Firefox window",
            "Read the label move Firefox to workspace two",
            "Find the text snap Firefox to the left side",
        ]
        for request in requests:
            with self.subTest(request=request):
                self.assertIsNone(self.planner.try_plan(request, "incidental"))

    def setUp(self) -> None:
        bus = PhaxEventBus()
        registry = ToolRegistry(
            ToolContext(
                {"security": {"max_tool_output_bytes": 10000}},
                bus,
                __import__("logging").getLogger("test"),
            )
        )
        names = {
            "desktop.window.resolve": Permission.SAFE,
            "desktop.output.resolve": Permission.SAFE,
            "desktop.window.move_resize": Permission.LOW_RISK,
            "desktop.window.move_to_output": Permission.LOW_RISK,
            "desktop.workspace.resolve": Permission.SAFE,
            "desktop.window.move_to_workspace": Permission.LOW_RISK,
            "desktop.window.layout": Permission.LOW_RISK,
            "desktop.window.undo_last": Permission.LOW_RISK,
            "desktop.window.close": Permission.SENSITIVE,
            "desktop.window.wait": Permission.SAFE,
            "holohand.set_paused": Permission.LOW_RISK,
            "workspaces.capture_current": Permission.LOW_RISK,
            "applications.list": Permission.SAFE,
            "applications.open": Permission.SAFE,
        }
        for name, permission in names.items():
            registry.register(
                ToolSpec(name, "TEST", name, permission, {"type": "object"}, lambda _a, _c: {})
            )

        async def requester(_payload: dict, _request_id: str | None) -> dict:
            return {"status": "completed", "result": {}}

        self.planner = TaskPlanner(registry, requester, bus, StateMachine(bus))

    def test_holohand_and_setup_commands_are_typed_and_dry_runnable(self):
        plan = self.planner.try_plan("pause holohand", "hand")
        self.assertIsNotNone(plan)
        self.assertEqual(plan.steps[0].arguments, {"paused": True})
        plan = self.planner.try_plan("dry run, resume hand gestures", "hand")
        self.assertTrue(plan.dry_run)
        self.assertEqual(plan.steps[0].arguments, {"paused": False})
        plan = self.planner.try_plan("Save this setup as BNM", "workspace")
        self.assertEqual(plan.steps[0].tool, "workspaces.capture_current")
        self.assertEqual(plan.steps[0].arguments, {"name": "BNM"})

    def test_geometry_request_composes_general_primitives(self) -> None:
        plan = self.planner.try_plan(
            "copy the size and coordinates of my terminal window, make Firefox match it, then close the terminal",
            "geometry",
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(
            [step.tool for step in plan.steps],
            [
                "desktop.window.resolve",
                "desktop.window.resolve",
                "desktop.window.move_resize",
                "desktop.window.close",
            ],
        )
        self.assertEqual(
            plan.steps[2].arguments["width"]["$ref"], "source.result.window.geometry.width"
        )
        self.assertEqual(plan.steps[-1].permission_class, "SENSITIVE")

    def test_open_on_monitor_is_not_application_specific(self) -> None:
        plan = self.planner.try_plan("throw Blender on my other monitor", "move")
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.steps[0].arguments["description"], "Blender")
        self.assertEqual(plan.steps[1].arguments["description"], "my other monitor")

    def test_dry_run_keeps_mutations_unexecuted(self) -> None:
        plan = self.planner.try_plan(
            "Don't actually do it, move Firefox to my laptop screen", "dry"
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertTrue(plan.dry_run)

    def test_natural_layout_and_undo_requests_use_verified_tools(self) -> None:
        centered = self.planner.try_plan("center Firefox", "center")
        assert centered is not None
        self.assertEqual(
            [step.tool for step in centered.steps],
            ["desktop.window.resolve", "desktop.window.layout"],
        )
        self.assertEqual(centered.steps[-1].arguments["layout"], "center")
        tiled = self.planner.try_plan("put Firefox on the left half of the screen", "tile")
        assert tiled is not None
        self.assertEqual(tiled.steps[-1].arguments["layout"], "left")
        undo = self.planner.try_plan("undo that", "undo")
        assert undo is not None
        self.assertEqual([step.tool for step in undo.steps], ["desktop.window.undo_last"])

    def test_workspace_move_resolves_real_workspace_before_mutation(self) -> None:
        plan = self.planner.try_plan("move Firefox to workspace 2", "workspace")
        assert plan is not None
        self.assertEqual(
            [step.tool for step in plan.steps],
            [
                "desktop.window.resolve",
                "desktop.workspace.resolve",
                "desktop.window.move_to_workspace",
            ],
        )

    def test_cancel_waiting_plan_revokes_planner_confirmation(self) -> None:
        async def requester(payload: dict, _request_id: str | None) -> dict:
            if payload["name"] == "desktop.window.close":
                return {"status": "confirmation_required", "confirmation": {"id": "confirm-close"}}
            if payload["name"] == "desktop.window.resolve":
                description = payload["arguments"]["description"]
                return {
                    "status": "completed",
                    "result": {
                        "resolved": True,
                        "window": {
                            "id": description,
                            "geometry": {"x": 0, "y": 0, "width": 100, "height": 100},
                        },
                    },
                }
            return {"status": "completed", "result": {"verified": True}}

        self.planner.request_tool = requester
        plan = self.planner.try_plan(
            "put Firefox exactly where Terminal is then close Terminal", "cancel"
        )
        assert plan is not None
        pending = asyncio.run(self.planner.execute(plan))
        self.assertEqual(pending["status"], "confirmation_required")
        result = self.planner.cancel("user_request")
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["confirmation_ids"], ["confirm-close"])
        self.assertIsNone(self.planner.active)
        self.assertNotIn("confirm-close", self.planner.pending)
        self.assertEqual(plan.status, "CANCELLED")

    def test_failure_builds_reproducible_report_and_classifies_ambiguity(self) -> None:
        calls = 0

        async def requester(payload: dict, _request_id: str | None) -> dict:
            nonlocal calls
            calls += 1
            if payload["name"] == "desktop.window.resolve":
                raise RuntimeError("Ambiguous window description: editor")
            return {"status": "completed", "result": {}}

        self.planner.request_tool = requester
        plan = self.planner.try_plan("move editor to my other monitor", "failure-report")
        assert plan is not None

        result = asyncio.run(self.planner.execute(plan))

        self.assertEqual(result["status"], "failed")
        self.assertEqual(calls, 2)
        self.assertEqual(plan.capability_gaps[0]["type"], "AMBIGUOUS_REQUEST")
        self.assertEqual(plan.recovery[0]["strategy"], "bounded_retry")
        self.assertEqual(plan.recovery[0]["outcome"], "FAILED")
        assert plan.failure_report is not None
        self.assertEqual(plan.failure_report["request"], "move editor to my other monitor")
        self.assertEqual(plan.failure_report["tool"], "desktop.window.resolve")
        self.assertEqual(plan.failure_report["expected_postcondition"], "One exact target window")
        self.assertEqual(plan.failure_report["attempts"], 2)
        self.assertEqual(plan.failure_report["arguments"], {"description": "editor"})
        self.assertIn("window", plan.timings)
        self.assertIn("total", plan.timings)
        failed = [event for event in self.planner.bus.history() if event["type"] == "plan.failed"]
        self.assertEqual(
            failed[0]["payload"]["failure_report"]["error"], "Ambiguous window description: editor"
        )

    def test_failed_observation_retains_world_state_and_real_error(self) -> None:
        async def requester(payload: dict, _request_id: str | None) -> dict:
            return {
                "status": "completed",
                "result": {
                    "resolved": False,
                    "error": "No window matches 'missing editor'",
                    "candidates": [],
                },
            }

        self.planner.request_tool = requester
        plan = self.planner.try_plan("move missing editor to my other monitor", "missing-window")
        assert plan is not None

        result = asyncio.run(self.planner.execute(plan))

        self.assertEqual(result["status"], "failed")
        self.assertEqual(plan.capability_gaps[0]["type"], "AMBIGUOUS_REQUEST")
        self.assertEqual(plan.steps[0].error, "No window matches 'missing editor'")
        self.assertEqual(plan.world_state_used["window"]["candidates"], [])
        assert plan.failure_report is not None
        self.assertEqual(plan.failure_report["world_state_used"], plan.world_state_used)


if __name__ == "__main__":
    unittest.main()
