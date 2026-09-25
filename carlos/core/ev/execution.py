"""Provider-independent boundary for actual registered-tool execution."""

from typing import Any
from contextvars import ContextVar

from .tools.base import ToolRegistry, ToolSpec
from .tools.results import ExecutionResult, evaluate_result

execution_correlation = ContextVar("ev_execution_correlation", default="")


class ExecutionController:
    """Called only after the service validates and authorizes the action.

    TaskPlanner retains ordering/recovery/cancellation. This boundary gives all
    callers the same evidence contract without inventing another tool backend.
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    async def execute(
        self, spec: ToolSpec, arguments: dict[str, Any]
    ) -> tuple[dict[str, Any], ExecutionResult]:
        data = await self.registry.execute(spec, arguments)
        return data, evaluate_result(spec.name, data, read_only=spec.read_only)
