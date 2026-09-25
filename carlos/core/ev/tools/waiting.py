import asyncio

from ..execution import execution_correlation
from ..permissions import Permission
from ..task_wait import wait_for_conditions
from .base import ToolSpec
from .builtin import object_schema


def register_waiting_tools(
    registry, requester, cancelled=lambda correlation: False, generation=lambda: 0
):
    async def wait(arguments, context):
        correlation = execution_correlation.get()
        started_generation = generation()

        def notify(phase, detail):
            context.bus.publish(
                "task.wait_state", "executor", {"phase": phase, **detail}, correlation
            )

        monitor = asyncio.create_task(
            wait_for_conditions(
                arguments["conditions"],
                requester,
                correlation,
                timeout=arguments.get("timeout_seconds", 60),
                interval=arguments.get("interval_seconds", 2),
                notify=notify,
            )
        )
        try:
            while not monitor.done():
                if cancelled(correlation) or generation() != started_generation:
                    return {
                        "ok": False,
                        "verified": False,
                        "wait_state": "CANCELLED",
                        "error": "Waiting cancelled; no action replayed.",
                    }
                await asyncio.wait({monitor}, timeout=0.1)
            return await monitor
        finally:
            if not monitor.done():
                monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)

    registry.register(
        ToolSpec(
            "agent.wait_for",
            "AGENT",
            "Wait for 1–3 concrete goal predicates by fresh read-only polling, up to 30 minutes. Never repeats the action that started the work. Use conditions from agent.verify_conditions. Timeout is not success; inspect and replan. Polling is bounded to 60 attempts. process_ended observes an exact previously recorded PID/start_ticks/boot_id, not exit-code or task success.",
            Permission.SAFE,
            object_schema(
                {
                    "conditions": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 3,
                        "items": {"type": "object"},
                    },
                    "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 1800},
                    "interval_seconds": {"type": "number", "minimum": 0.5, "maximum": 60},
                },
                ["conditions"],
            ),
            wait,
            read_only=True,
            timeout_seconds=1810,
            verification="Declared predicates freshly observed after waiting",
        )
    )
