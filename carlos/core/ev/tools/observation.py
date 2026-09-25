from ..desktop.observation import DesktopObservation
from ..permissions import Permission
from .base import ToolRegistry, ToolSpec
from .builtin import object_schema


def register_observation_tools(registry: ToolRegistry) -> None:
    def capabilities(arguments, context):
        from ..capabilities import capability_evidence

        query = arguments["query"].casefold().strip()
        catalog = registry.catalog()
        matched = [
            item
            for item in catalog
            if query
            in (item["name"] + " " + item["category"] + " " + item["description"]).casefold()
        ]
        limit = arguments.get("limit", 12)
        rows = capability_evidence(
            matched[:limit], context.bus.history(1000), context.desktop.input.status()
        )
        fields = (
            "name",
            "category",
            "description",
            "requires_confirmation",
            "implementation",
            "backend_state",
            "execution_evidence",
        )
        return {
            "capabilities": [{key: row[key] for key in fields} for row in rows],
            "matches": len(matched),
            "more_available": len(matched) > limit,
            "scope": "Registration and bounded observations, not proof any arbitrary target will work",
        }

    registry.register(
        ToolSpec(
            "agent.capabilities",
            "AGENT",
            "Inspect capability/backend readiness and recent accepted/failed/verified tool evidence for one keyword or exact tool name. Unknown is not working; old successes are not current target proof. This performs no input, connection, or setting change.",
            Permission.SAFE,
            object_schema(
                {
                    "query": {"type": "string", "minLength": 1, "maxLength": 150},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                ["query"],
            ),
            capabilities,
            read_only=True,
        )
    )

    async def observe(arguments, context):
        return await DesktopObservation(context.desktop, context.accessibility).observe(**arguments)

    registry.register(
        ToolSpec(
            "desktop.observe",
            "DESKTOP",
            "Observe current windows, exact active/target window, outputs, cursor and input readiness together. "
            "Use basic first; accessibility adds bounded controls scoped to the exact PID/title with a freshness check. "
            "Does not click, connect input, take screenshots or read other windows' controls. "
            "Inspect accessibility status/warnings: partial or stale evidence is not a usable target.",
            Permission.SAFE,
            object_schema(
                {
                    "window_id": {"type": "string", "maxLength": 100},
                    "level": {"type": "string", "enum": ["basic", "accessibility"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 150},
                    "fresh": {"type": "boolean"},
                },
                [],
            ),
            observe,
            read_only=True,
            timeout_seconds=16,
            verification="Scoped desktop evidence returned; readiness and semantic completeness reported separately",
        )
    )
