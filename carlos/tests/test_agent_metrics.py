import unittest
from ev.events import PhaxEventBus


class AgentMetricsTests(unittest.TestCase):
    def test_nested_plan_latency_does_not_double_count_children(self):
        bus = PhaxEventBus()
        bus.publish("tool.started", "tools", {"tool": "agent.execute_plan"}, "task")
        bus.publish(
            "tool.completed",
            "tools",
            {"tool": "files.hash", "ok": True, "execution": {"verified": True}},
            "task",
            10,
        )
        bus.publish(
            "tool.completed", "tools", {"tool": "agent.execute_plan", "ok": True}, "task", 25
        )
        bus.publish("plan.goal_verified", "planner", {"verified": True}, "task")
        bus.publish("plan.step_completed", "planner", {"step": {"attempts": 2}}, "task")
        report = bus.latency_report()["recent"][0]
        self.assertEqual(report["stages_ms"]["tool_execution_and_verification"], 10)
        self.assertEqual(report["counts"]["leaf_tool_results"], 1)
        self.assertEqual(report["counts"]["retries_in_completed_steps"], 1)
        self.assertEqual(report["counts"]["declared_goal_checks_passed"], 1)
        self.assertIsNone(report["false_success_rate"])

    def test_failed_payload_is_counted_even_when_dispatch_completed(self):
        bus = PhaxEventBus()
        bus.publish("tool.started", "tools", {"tool": "files.hash"}, "task")
        bus.publish("tool.completed", "tools", {"tool": "files.hash", "ok": False}, "task", 3)
        self.assertEqual(bus.latency_report()["recent"][0]["counts"]["failed_leaf_tools"], 1)
