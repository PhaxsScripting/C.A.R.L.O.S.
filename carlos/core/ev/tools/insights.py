from .base import ToolSpec
from .builtin import object_schema
from ..permissions import Permission


def register_insight_tools(registry, insights):
    registry.register(
        ToolSpec(
            "agent.insights",
            "AGENT",
            "Read local telemetry-based assistance cards. Suggestions are not executed, approved, or diagnosed root causes.",
            Permission.SAFE,
            object_schema({}),
            lambda a, c: {"items": insights.snapshot(), "actions_executed": 0},
            read_only=True,
        )
    )

    def dismiss(a, c):
        result = insights.dismiss(a["id"])
        c.bus.publish("agent.insights_changed", "insights", {"items": insights.snapshot()})
        return result

    registry.register(
        ToolSpec(
            "agent.insights.dismiss",
            "AGENT",
            "Hide one local assistance card for one hour. Does not fix or change the underlying system condition.",
            Permission.LOW_RISK,
            object_schema(
                {"id": {"type": "string", "enum": ["thermal", "memory", "storage", "battery"]}},
                ["id"],
            ),
            dismiss,
        )
    )
