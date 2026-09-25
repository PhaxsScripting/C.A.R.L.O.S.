"""Numbered, window-bound semantic controls for hands-free navigation."""

from __future__ import annotations
import asyncio

from .base import ToolRegistry, ToolSpec, validate_schema
from .builtin import object_schema, _active_accessibility_scope
from ..permissions import Permission

_IDENTITY_TEXT = {"type": "string", "minLength": 1, "maxLength": 500}
_IDENTITY_FIELDS = {
    "window_id": _IDENTITY_TEXT,
    "process_id": {"type": "integer", "minimum": 1},
    "window_title": _IDENTITY_TEXT,
    "path": _IDENTITY_TEXT,
    "name": _IDENTITY_TEXT,
    "role": _IDENTITY_TEXT,
    "application": _IDENTITY_TEXT,
}
CONTROL_IDENTITY_SCHEMA = object_schema(_IDENTITY_FIELDS, list(_IDENTITY_FIELDS))


async def controls_list(a, c):
    pid, title = await _active_accessibility_scope(a, c)
    listing = await asyncio.to_thread(
        c.accessibility.list_elements, "", a.get("query", ""), a.get("role", ""), 200, pid, title
    )
    if await _active_accessibility_scope(a, c) != (pid, title):
        raise ValueError("The window changed while reading its controls; ask again")
    items = []
    for element in listing.get("elements", []):
        states = element.get("states", {})
        selectable = states.get("selectable") is True or (
            states.get("selected") is not None
            and element.get("role", "").casefold()
            in {"table cell", "page tab", "list item", "menu item"}
        )
        if (
            not element.get("name")
            or not (
                element.get("actions")
                or element.get("editable_text")
                or element.get("value_control")
                or selectable
            )
            or not element.get("path")
        ):
            continue
        items.append(
            {
                "id": element["path"],
                "title": element["name"],
                "name": element["name"],
                "role": element["role"],
                "application": element["application"],
                "window_id": a["window_id"],
                "process_id": pid,
                "window_title": title,
                "states": element.get("states", {}),
                "editable_text": element.get("editable_text", False),
                "value_control": element.get("value_control", False),
            }
        )
    truncated = bool(listing.get("truncated") or listing.get("errors") or len(items) > 30)
    items = items[:30]
    message = (
        "; ".join(f"{i+1}. {item['name']} ({item['role']})" for i, item in enumerate(items[:12]))
        or "This window exposes no matching actionable controls. Try a specific control name or its native settings page."
    )
    if truncated:
        message += " The control scan is partial; narrow it by name if necessary."
    return {"items": items, "choice_kind": "control", "message": message, "truncated": truncated}


async def controls_activate(a, c):
    pid, title = await _active_accessibility_scope(a, c)
    if (pid, title) != (a["process_id"], a["window_title"]):
        raise ValueError(
            "That numbered control belongs to an older window/page. List controls again."
        )
    listing = await asyncio.to_thread(
        c.accessibility.list_elements, a["application"], a["name"], a["role"], 200, pid, title
    )
    matches = [
        e
        for e in listing.get("elements", [])
        if e.get("name") == a["name"] and e.get("role") == a["role"]
    ]
    if (
        listing.get("truncated")
        or listing.get("errors")
        or len(matches) != 1
        or matches[0].get("path") != a["path"]
        or not matches[0].get("actions")
    ):
        raise ValueError("That control changed or is ambiguous. No click was sent.")
    if await _active_accessibility_scope(a, c) != (pid, title):
        raise ValueError("The window changed before control activation")
    result = await asyncio.to_thread(
        c.accessibility.activate, a["application"], a["name"], a["role"], pid, title
    )
    accepted = result.get("action_accepted", result.get("verified")) is True
    return {
        "verified": False,
        "action_accepted": accepted,
        "message": (
            f"Activated {a['name']}. The resulting application state is not yet verified."
            if accepted
            else "The control rejected activation."
        ),
    }


async def controls_resolve(a, c):
    """Return one typed identity, never a best guess or a list-position click."""
    listing = await controls_list(
        {"window_id": a["window_id"], "query": a["name"], "role": a["role"]}, c
    )
    matches = [
        item for item in listing["items"] if item["name"] == a["name"] and item["role"] == a["role"]
    ]
    if listing.get("truncated") or len(matches) != 1:
        raise ValueError(
            "An exact unique control could not be resolved from a complete scan; no action was sent"
        )
    item = matches[0]
    target = {key: item["id"] if key == "path" else item[key] for key in _IDENTITY_FIELDS}
    validate_schema(target, CONTROL_IDENTITY_SCHEMA)
    return {
        "target": target,
        "resolved": True,
        "changed": False,
        "note": "Observed identity only. Each action rechecks it; resolution does not prove action success.",
    }


async def control_details(a, c):
    scope = await _active_accessibility_scope(a, c)
    if scope != (a["process_id"], a["window_title"]):
        raise ValueError("Control belongs to an older window; inspect again")
    result = await asyncio.to_thread(
        c.accessibility.inspect_control, a["application"], a["name"], a["role"], *scope, a["path"]
    )
    if await _active_accessibility_scope(a, c) != scope:
        raise ValueError("Window changed while reading control state")
    return result


async def control_set(a, c):
    scope = await _active_accessibility_scope(a, c)
    if scope != (a["process_id"], a["window_title"]):
        raise ValueError("Control belongs to an older window; inspect again")
    result = await asyncio.to_thread(
        c.accessibility.set_control,
        a["application"],
        a["name"],
        a["role"],
        *scope,
        a["path"],
        a["state"],
        a["value"],
    )
    if await _active_accessibility_scope(a, c) != scope:
        return {
            "verified": False,
            "error": "Window changed during control action; effect is uncertain",
        }
    return result


async def control_text(a, c):
    scope = await _active_accessibility_scope(a, c)
    if scope != (a["process_id"], a["window_title"]):
        raise ValueError("Control belongs to an older window; inspect again")
    result = await asyncio.to_thread(
        c.accessibility.replace_control_text,
        a["application"],
        a["name"],
        a["role"],
        *scope,
        a["path"],
        a["expected_text"],
        a["text"],
    )
    if await _active_accessibility_scope(a, c) != scope:
        return {
            "verified": False,
            "error": "Window changed during text entry; effect is uncertain",
            "submitted": False,
        }
    return result


async def control_type_empty(a, c):
    """Explicit keyboard alternative, never an automatic retry of a setter."""
    text = a["text"]
    if not text or len(text) > 2000 or any(not char.isprintable() for char in text):
        raise ValueError("Only printable text is allowed; no Enter or Tab")
    scope = await _active_accessibility_scope(a, c)
    if scope != (a["process_id"], a["window_title"]):
        raise ValueError("Control belongs to an older window; inspect again")
    if not c.desktop.input.status().get("connected"):
        raise RuntimeError(
            "Native input session is not connected; explicitly connect it before typing"
        )
    await asyncio.to_thread(
        c.accessibility.focus_empty_control,
        a["application"],
        a["name"],
        a["role"],
        *scope,
        a["path"],
    )

    async def guard(offset):
        # Short read-only polling accommodates asynchronous focus/text events.
        # A different nonempty value or loss of focus always stops, never erases.
        for attempt in range(5):
            if await _active_accessibility_scope(a, c) != scope:
                raise RuntimeError("Window changed while typing; input stopped")
            observed = await asyncio.to_thread(
                c.accessibility.inspect_control,
                a["application"],
                a["name"],
                a["role"],
                *scope,
                a["path"],
            )
            states = observed.get("element", {}).get("states", {})
            actual = observed.get("text")
            if (
                observed.get("text_available") is True
                and not observed.get("text_truncated")
                and actual == text[:offset]
                and states.get("read_only") is not True
                and all(
                    states.get(key) is True
                    for key in ("enabled", "sensitive", "showing", "focused", "editable")
                )
            ):
                return
            if not isinstance(actual, str) or not text[:offset].startswith(actual):
                raise RuntimeError("Field contents changed; input stopped without overwriting")
            if attempt < 4:
                await asyncio.sleep(0.05)
        raise RuntimeError(
            "Exact field focus/text progress could not be verified; no further input sent"
        )

    result = await c.desktop.input.type_text(a["window_id"], text, control_guard=guard)
    await guard(len(text))
    return {
        **result,
        "verified": True,
        "submitted": False,
        "verification_scope": "Exact empty field keyboard entry and text readback; no submit/save",
    }


def register_controls_tools(registry: ToolRegistry):
    text = {"type": "string", "minLength": 1, "maxLength": 500}
    identity = _IDENTITY_FIELDS
    registry.register(
        ToolSpec(
            "desktop.controls.resolve",
            "ACCESSIBILITY",
            "Resolve one exact observed control name and role in a focused window into a typed target identity. Rejects partial scans, missing controls and duplicates; never clicks. Use target fields for control actions, or a prior-step target reference for control_text/control_state conditions. An observed identity can go stale and is rechecked by each action.",
            Permission.SAFE,
            object_schema(
                {"window_id": text, "name": text, "role": text}, ["window_id", "name", "role"]
            ),
            controls_resolve,
            read_only=True,
        )
    )
    registry.register(
        ToolSpec(
            "desktop.controls.type_empty",
            "ACCESSIBILITY",
            "Keyboard alternative for a freshly observed EMPTY non-password editable field when native text setters are unavailable or ineffective. Requires an already connected OS input session. Focuses the exact field, verifies text/focus every eight characters and final contents. Never overwrites existing text, presses Enter, saves or submits. No automatic retry after a failed write; inspect current contents first.",
            Permission.SENSITIVE,
            object_schema(
                {**identity, "text": {"type": "string", "minLength": 1, "maxLength": 2000}},
                [*identity, "text"],
            ),
            control_type_empty,
        )
    )
    registry.register(
        ToolSpec(
            "desktop.controls.set_text",
            "ACCESSIBILITY",
            "Fill an exact observed editable field only while its complete current text matches expected_text. Obtain identity/text from controls.inspect. Rejects passwords, stale identity, disabled fields and partial text. Independently reads back; never presses Enter, submits, saves a document or confirms a dialog.",
            Permission.SENSITIVE,
            object_schema(
                {
                    **identity,
                    "expected_text": {"type": "string", "maxLength": 2000},
                    "text": {"type": "string", "maxLength": 2000},
                },
                [*identity, "expected_text", "text"],
            ),
            control_text,
            cancellable=False,
        )
    )
    registry.register(
        ToolSpec(
            "desktop.controls.inspect",
            "ACCESSIBILITY",
            "Read the exact previously observed control's state and bounded text/value. Requires unchanged window PID/title and node path. Does not inspect password text.",
            Permission.SAFE,
            object_schema(identity, list(identity)),
            control_details,
            read_only=True,
        )
    )
    registry.register(
        ToolSpec(
            "desktop.controls.set_state",
            "ACCESSIBILITY",
            "Set and read back an exact checkbox/radio/toggle checked state, tab/list/table-cell selection, or slider/spin value. Requires unchanged window PID/title, control path and enabled/sensitive state; refuses missing evidence. No generic confirmation-dialog clicking.",
            Permission.SENSITIVE,
            object_schema(
                {
                    **identity,
                    "state": {"type": "string", "enum": ["checked", "selected", "value"]},
                    "value": {},
                },
                [*identity, "state", "value"],
            ),
            control_set,
            cancellable=False,
        )
    )
    registry.register(
        ToolSpec(
            "desktop.controls.list",
            "ACCESSIBILITY",
            "List numbered native controls bound to one exact focused window. Does not click or collect screenshots.",
            Permission.SAFE,
            object_schema(
                {
                    "window_id": text,
                    "query": {"type": "string", "maxLength": 100},
                    "role": {"type": "string", "maxLength": 100},
                },
                ["window_id"],
            ),
            controls_list,
        )
    )
    registry.register(
        ToolSpec(
            "desktop.controls.activate",
            "ACCESSIBILITY",
            "Activate one previously listed semantic control only if PID, window title, node path, name and role still uniquely match. Action acceptance is not outcome verification.",
            Permission.SENSITIVE,
            object_schema(
                {
                    "window_id": text,
                    "process_id": {"type": "integer", "minimum": 1},
                    "window_title": text,
                    "path": text,
                    "name": text,
                    "role": text,
                    "application": text,
                },
                ["window_id", "process_id", "window_title", "path", "name", "role", "application"],
            ),
            controls_activate,
            cancellable=False,
        )
    )
