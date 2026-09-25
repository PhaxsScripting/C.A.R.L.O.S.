"""Read-only inspection of execution receipts, not executable saved plans."""

from ..permissions import Permission
from .base import ToolRegistry, ToolSpec
from .builtin import object_schema


def register_task_history_tools(registry: ToolRegistry) -> None:
    def recent(arguments, context):
        return {
            "tasks": context.task_journal.recent(arguments.get("limit", 10)),
            "historical": True,
            "requires_fresh_observation_before_actions": True,
        }

    def detail(arguments, context):
        task = context.task_journal.get_page(
            arguments["id"],
            arguments.get("offset", 0),
            arguments.get("limit", 5),
            arguments.get("through_step_id"),
        )
        if task is None:
            return {"ok": False, "error": "No task has that exact ID"}
        return {
            "task": task,
            "historical": True,
            "saved_approvals_reusable": False,
            "requires_fresh_observation_before_actions": True,
        }

    registry.register(
        ToolSpec(
            "agent.history",
            "AGENT",
            "Inspect recent task attempts and statuses to explain what E.V. actually did. Historical data, not proof of current desktop state.",
            Permission.SAFE,
            object_schema({"limit": {"type": "integer", "minimum": 1, "maximum": 20}}, []),
            recent,
            read_only=True,
        )
    )
    registry.register(
        ToolSpec(
            "agent.task_status",
            "AGENT",
            "Read one exact historical task ID and a page of recorded tool results (default five). Follow next_step_offset until null, passing returned steps_through_id as through_step_id to freeze the step-list boundary while new steps append. Omitted steps are not absent. Narrow limit to one if a page is too large. Running step statuses may change between pages. Task-level duplicate tool receipts are omitted. Interrupted steps may already have affected the computer; never blindly replay them. Saved approvals are never reusable.",
            Permission.SAFE,
            object_schema(
                {
                    "id": {"type": "string", "minLength": 1, "maxLength": 100},
                    "offset": {"type": "integer", "minimum": 0, "maximum": 256},
                    "through_step_id": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 9223372036854775807,
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                ["id"],
            ),
            detail,
            read_only=True,
        )
    )
