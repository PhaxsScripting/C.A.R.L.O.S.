from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..events import PhaxEventBus
from ..permissions import Permission
from .results import OBSERVATION_TOOLS


class ValidationError(ValueError):
    pass


Executor = Callable[[dict[str, Any], "ToolContext"], dict[str, Any] | Awaitable[dict[str, Any]]]
Normalizer = Callable[[dict[str, Any], "ToolContext"], dict[str, Any]]


@dataclass(slots=True)
class ToolContext:
    config: dict[str, Any]
    bus: PhaxEventBus
    logger: logging.Logger
    memory: Any = None
    desktop: Any = None
    security_center: Any = None
    coding_agent: Any = None
    accessibility: Any = None
    vision: Any = None
    daily: Any = None
    power: Any = None
    spotify: Any = None
    media_focus: Any = None
    task_journal: Any = None
    capability_probe: Any = None


@dataclass(slots=True)
class ToolSpec:
    name: str
    category: str
    description: str
    permission: Permission
    schema: dict[str, Any]
    executor: Executor
    normalizer: Normalizer | None = None
    confirmation_reason: str = "This action needs confirmation."
    timeout_seconds: float = 30.0
    output_schema: dict[str, Any] | None = None
    platform_requirements: tuple[str, ...] = ()
    availability: str = "REGISTERED_UNCHECKED"
    verification: str = "Tool-specific postcondition"
    side_effects: tuple[str, ...] = ()
    cancellable: bool = True
    expected_latency_ms: int = 500
    requires_confirmation: bool = False
    read_only: bool = False
    offline_available: bool | None = None
    reversible: bool | None = None
    required_capabilities: tuple[str, ...] = ()

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "read_only": self.read_only,
            "offline_available": self.offline_available,
            "reversible": self.reversible,
            "category": self.category,
            "description": self.description,
            "permission": self.permission.value,
            "schema": self.schema,
            "requires_confirmation": self.requires_confirmation,
            "confirmation_reason": self.confirmation_reason if self.requires_confirmation else "",
            "output_schema": self.output_schema or {"type": "object"},
            "platform_requirements": list(self.platform_requirements),
            "required_capabilities": list(self.required_capabilities),
            "availability": self.availability,
            "verification": self.verification,
            "side_effects": list(self.side_effects),
            "cancellable": self.cancellable,
            "expected_latency_ms": self.expected_latency_ms,
        }


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "null":
        return value is None
    return False


def validate_schema(value: Any, schema: dict[str, Any], path: str = "arguments") -> Any:
    expected = schema.get("type")
    if expected and not _type_matches(value, expected):
        raise ValidationError(f"{path} must be {expected}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValidationError(f"{path} must be one of {schema['enum']}")
    if isinstance(value, str):
        if len(value) < int(schema.get("minLength", 0)):
            raise ValidationError(f"{path} is too short")
        if len(value) > int(schema.get("maxLength", 1_000_000)):
            raise ValidationError(f"{path} is too long")
        if "pattern" in schema and re.fullmatch(str(schema["pattern"]), value) is None:
            raise ValidationError(f"{path} has an invalid format")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValidationError(f"{path} must be finite")
        if "minimum" in schema and value < schema["minimum"]:
            raise ValidationError(f"{path} is below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValidationError(f"{path} exceeds maximum")
    if isinstance(value, list):
        if len(value) < int(schema.get("minItems", 0)):
            raise ValidationError(f"{path} has too few items")
        if len(value) > int(schema.get("maxItems", 10_000)):
            raise ValidationError(f"{path} has too many items")
        if "items" in schema:
            for index, item in enumerate(value):
                validate_schema(item, schema["items"], f"{path}[{index}]")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                raise ValidationError(f"{path}.{key} is required")
        if schema.get("additionalProperties") is False:
            unknown = set(value) - set(properties)
            if unknown:
                raise ValidationError(f"{path} contains unknown fields: {sorted(unknown)}")
        for key, item in value.items():
            if key in properties:
                validate_schema(item, properties[key], f"{path}.{key}")
    return value


class ToolRegistry:
    def __init__(self, context: ToolContext) -> None:
        self.context = context
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"duplicate tool: {spec.name}")
        if spec.name in OBSERVATION_TOOLS:
            spec.read_only = True
        if (
            self.context.config.get("carlos", {}).get("strict_permissions", False)
            and spec.permission in {Permission.HIGH, Permission.PRIVILEGED, Permission.DESTRUCTIVE}
            and not spec.read_only
        ):
            spec.requires_confirmation = True
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as error:
            raise ValidationError(f"unknown tool: {name}") from error

    def catalog(self) -> list[dict[str, Any]]:
        return [
            spec.public()
            for spec in sorted(self._tools.values(), key=lambda item: (item.category, item.name))
        ]

    def validate(self, name: str, arguments: Any) -> tuple[ToolSpec, dict[str, Any]]:
        spec = self.get(name)
        if not isinstance(arguments, dict):
            raise ValidationError("tool arguments must be an object")
        validate_schema(arguments, spec.schema)
        normalized = (
            spec.normalizer(dict(arguments), self.context) if spec.normalizer else arguments
        )
        validate_schema(normalized, spec.schema)
        return spec, normalized

    async def execute(self, spec: ToolSpec, arguments: dict[str, Any]) -> dict[str, Any]:
        if spec.required_capabilities:
            if self.context.capability_probe is None:
                raise ValidationError("Capability probe is unavailable")
            evidence = await self.context.capability_probe()
            rows = evidence.get("capabilities", {})
            missing = [
                name
                for name in spec.required_capabilities
                if rows.get(name, {}).get("state")
                not in {"READY", "AVAILABLE", "CONNECTED", "ACTIVE"}
            ]
            if missing:
                raise ValidationError("Required capabilities unavailable: " + ", ".join(missing))
        if inspect.iscoroutinefunction(spec.executor):
            result = await asyncio.wait_for(
                spec.executor(arguments, self.context), timeout=spec.timeout_seconds
            )
        else:
            result = await asyncio.wait_for(
                asyncio.to_thread(spec.executor, arguments, self.context),
                timeout=spec.timeout_seconds,
            )
        if not isinstance(result, dict):
            raise TypeError(f"tool {spec.name} returned a non-object")
        if spec.output_schema is not None:
            validate_schema(result, spec.output_schema, "result")
        maximum = int(self.context.config["security"]["max_tool_output_bytes"])
        encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
        if len(encoded) > maximum:
            return {
                "truncated": True,
                "original_bytes": len(encoded),
                "summary": str(result)[: maximum // 2],
            }
        return result
