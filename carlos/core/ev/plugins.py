"""Explicitly enabled, installed Python integrations using the ordinary tool gates.

Entry points are trusted user-installed code, not sandboxed third-party scripts.
Discovery never imports an integration unless its exact name is enabled.
"""

from importlib.metadata import entry_points
import inspect
import re

from .permissions import Permission
from .tools.base import ToolSpec


def load_enabled_plugins(enabled, registry):
    if (
        not isinstance(enabled, list)
        or len(enabled) > 8
        or any(
            not isinstance(n, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", n) for n in enabled
        )
    ):
        return [{"state": "BLOCKED", "reason": "Invalid enabled-plugin list"}]
    if not enabled:
        return []
    try:
        installed = list(entry_points(group="carlos.tools"))
    except Exception as error:
        return [
            {"state": "FAILED", "reason": "Integration discovery failed: " + type(error).__name__}
        ]
    results = []
    for name in dict.fromkeys(enabled):
        matches = [item for item in installed if item.name == name]
        if len(matches) != 1:
            results.append(
                {
                    "name": name,
                    "state": "BLOCKED",
                    "reason": "Installed integration is missing or ambiguous",
                }
            )
            continue
        try:
            specs = matches[0].load()()
            if not isinstance(specs, (list, tuple)) or not 1 <= len(specs) <= 32:
                raise ValueError("Integration must return 1-32 tool declarations")
            seen = set()
            for spec in specs:
                if (
                    not isinstance(spec, ToolSpec)
                    or not spec.name.startswith(f"plugin.{name}.")
                    or not re.fullmatch(r"[a-z][a-z0-9_.-]{1,127}", spec.name)
                    or spec.name in seen
                    or spec.name in registry._tools
                ):
                    raise ValueError("Invalid, duplicate or unscoped tool name")
                seen.add(spec.name)
                if (
                    not isinstance(spec.permission, Permission)
                    or not spec.description
                    or spec.schema.get("type") != "object"
                    or not spec.output_schema
                    or spec.output_schema.get("type") != "object"
                    or type(spec.offline_available) is not bool
                    or type(spec.reversible) is not bool
                    or type(spec.cancellable) is not bool
                    or not isinstance(spec.required_capabilities, tuple)
                    or any(
                        not isinstance(item, str) or not item for item in spec.required_capabilities
                    )
                ):
                    raise ValueError("Incomplete integration contract")
                if spec.cancellable and not inspect.iscoroutinefunction(spec.executor):
                    raise ValueError("Cancellable integrations require an async executor")
                if not 0 < spec.timeout_seconds <= 120:
                    raise ValueError("Integration timeout must be bounded")
            for spec in specs:
                registry.register(spec)
            results.append(
                {"name": name, "state": "REGISTERED", "tools": sorted(seen), "protocol_version": 1}
            )
        except Exception as error:
            results.append({"name": name, "state": "FAILED", "reason": type(error).__name__})
    return results
