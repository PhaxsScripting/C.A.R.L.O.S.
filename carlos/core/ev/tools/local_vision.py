"""Private local visual inference; deliberately not a GUI-action verifier."""

from ..permissions import Permission
from .base import ToolSpec
from .builtin import object_schema


def register_local_vision_tools(registry):
    def require_local(context):
        # Tool outputs are returned to the active reasoning model. Prevent a
        # cloud model from indirectly receiving private screen descriptions.
        active = context.config.get("providers", {}).get("active", "offline")
        if active not in {"offline", "local_hybrid", "local_agent"}:
            raise ValueError(
                "Private visual descriptions require a local active brain; cloud tool-result upload is blocked"
            )

    async def describe(arguments, context):
        require_local(context)
        return await context.vision.describe(arguments["capture_id"], arguments["question"])

    async def inspect_window(arguments, context):
        require_local(context)
        return await context.vision.inspect_window(arguments["window_id"], arguments["question"])

    registry.register(
        ToolSpec(
            "vision.describe",
            "VISION",
            "Ask the local vision model about one existing private capture. Returns uncertain visual inference, not verified facts or permission to click. No screenshots/descriptions are sent to cloud models. Requires an active local brain and configured multimodal weights.",
            Permission.SENSITIVE,
            object_schema(
                {
                    "capture_id": {"type": "string", "pattern": "[0-9a-f]{32}"},
                    "question": {"type": "string", "minLength": 1, "maxLength": 1000},
                },
                ["capture_id", "question"],
            ),
            describe,
            read_only=True,
            timeout_seconds=100,
            verification="Local model inference only; scene accuracy not independently verified",
            side_effects=("temporarily loads bounded local multimodal runtime",),
            expected_latency_ms=30000,
        )
    )

    registry.register(
        ToolSpec(
            "vision.inspect_window",
            "VISION",
            "Inspect one exact KWin window using a temporary local screenshot and multimodal model; delete the capture afterward. Rechecks window PID/title/geometry after inference and marks stale changes. This is uncertain historical image interpretation, not coordinates or permission to click. Requires local active brain.",
            Permission.SENSITIVE,
            object_schema(
                {
                    "window_id": {"type": "string", "minLength": 1, "maxLength": 100},
                    "question": {"type": "string", "minLength": 1, "maxLength": 1000},
                },
                ["window_id", "question"],
            ),
            inspect_window,
            timeout_seconds=115,
            verification="Visual inference only; exact target identity rechecked, scene truth unverified",
            side_effects=(
                "temporarily focuses exact window for capture",
                "creates and deletes one private screenshot",
                "temporarily loads local model",
            ),
            expected_latency_ms=30000,
        )
    )
