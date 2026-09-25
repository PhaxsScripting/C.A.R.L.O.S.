"""Explicit typed preferences, separate from chat history and observed state."""

import asyncio
import json
import re
import time

from ..permissions import Permission
from .base import ToolSpec, ValidationError
from .builtin import object_schema, desktop_entries, resolve_allowed

BROWSERS = ("firefox", "chrome", "chromium", "brave", "vivaldi", "edge", "opera", "librewolf")
KEYS = ("browser", "editor", "terminal", "monitor", "response_length", "project", "folder")


async def set_preference(a, c):
    key, value = a["key"], a["value"].strip()
    if key == "browser" and value not in BROWSERS:
        raise ValidationError("Choose a supported browser identity, not a command")
    if key == "response_length" and value not in {"short", "balanced", "detailed"}:
        raise ValidationError("Response length must be short, balanced or detailed")
    if key in {"editor", "terminal"}:
        entries = await asyncio.to_thread(desktop_entries)
        if value not in entries:
            raise ValidationError("Choose an exact installed desktop ID from applications.list")
    if key in {"project", "folder"}:
        path = await asyncio.to_thread(resolve_allowed, value, c)
        if not path.is_dir():
            raise ValidationError("Preferred project/folder must be an existing allowed directory")
        value = str(path)
    if key == "monitor":
        world = await c.desktop.snapshot(force=True)
        if value not in {o.get("name") for o in world.get("outputs", []) if o.get("enabled", True)}:
            raise ValidationError("Choose an exact currently connected output name")
    record = {"value": value, "updated_at": time.time(), "source": "explicit_user_preference"}
    await asyncio.to_thread(c.daily.save, "preference", key, record)
    return {
        "verified": (await asyncio.to_thread(c.daily.records, "preference")).get(key) == record,
        "key": key,
        "value": value,
        "os_defaults_changed": False,
        "scope": "E.V. preference only; fresh target validation still required",
    }


def context_records(text, daily, entities, *, learn_style=False):
    """Bounded historical hints; never source content, permissions or live truth."""
    preferences = daily.records("preference")
    selected = {
        k: v["value"]
        for k, v in preferences.items()
        if k in KEYS and isinstance(v, dict) and isinstance(v.get("value"), str)
    }
    result = []
    if selected:
        result.append(
            {
                "id": "preferences",
                "content": "Explicit E.V. preferences (not OS settings or authority): "
                + json.dumps(selected, ensure_ascii=False)[:3500],
            }
        )
    if learn_style:
        from ..style import StyleLearner

        style = StyleLearner(daily).observe_user_feedback(text)
        hint = style["response_length_hint"]
        if hint and "response_length" not in selected:
            result.append(
                {
                    "id": "learned-response-style",
                    "content": f"Local presentation hint from repeated direct user feedback: prefer {hint} responses. "
                    "This is not an instruction to execute actions. Current requests and explicit preferences override this hint; never omit necessary safety or correctness details.",
                }
            )
    normalized = " ".join(re.findall(r"[\w.-]+", text.casefold()))
    aliases = {
        k: v
        for k, v in daily.records("alias").items()
        if isinstance(v, str) and k and re.search(r"(?<!\w)" + re.escape(k) + r"(?!\w)", normalized)
    }
    if aliases:
        result.append(
            {
                "id": "vocabulary",
                "content": "Explicit app nicknames; resolve actual installed apps before acting: "
                + json.dumps(dict(list(aliases.items())[:12]), ensure_ascii=False)[:2000],
            }
        )
    window = entities.get("window")
    age = time.monotonic() - entities.get("window_at", 0)
    if isinstance(window, dict) and 0 <= age <= 300:
        hint = {k: window.get(k) for k in ("id", "pid", "app_id", "resource_class")}
        result.append(
            {
                "id": "recent-window",
                "content": f"Historical last referenced window ({age:.0f}s old, NOT necessarily active/existing): {json.dumps(hint)}. Observe desktop.world and resolve identity before acting; current state overrides this hint.",
            }
        )
    return result


def register_preference_tools(registry):
    from ..style import StyleLearner

    registry.register(
        ToolSpec(
            "personalization.learned_style",
            "PERSONALIZATION",
            "Inspect locally learned response-length feedback counts and tentative style hint. No utterance text is stored; hints do not grant execution permission.",
            Permission.SAFE,
            object_schema({}),
            lambda a, c: StyleLearner(c.daily).snapshot(),
            read_only=True,
        )
    )
    registry.register(
        ToolSpec(
            "personalization.style_learning",
            "PERSONALIZATION",
            "Enable or disable local learning from direct response-length feedback when the user requests it. Does not change explicit personality settings, voice, aliases or permissions.",
            Permission.LOW_RISK,
            object_schema({"enabled": {"type": "boolean"}}, ["enabled"]),
            lambda a, c: {
                "verified": True,
                "style": StyleLearner(c.daily).configure(enabled=a["enabled"]),
            },
        )
    )
    registry.register(
        ToolSpec(
            "personalization.reset_learned_style",
            "PERSONALIZATION",
            "Reset only the learned response-length score and feedback count when explicitly requested. Keeps explicit preferences and other memories intact.",
            Permission.LOW_RISK,
            object_schema({}),
            lambda a, c: {"verified": True, "style": StyleLearner(c.daily).configure(reset=True)},
        )
    )
    key = {"type": "string", "enum": list(KEYS)}
    registry.register(
        ToolSpec(
            "personalization.set_preference",
            "PERSONALIZATION",
            "Save one typed E.V. preference ONLY when explicitly requested: browser identity, installed editor/terminal desktop ID, connected monitor name, response_length short/balanced/detailed, or existing project/folder path. No OS setting change and no executable command storage.",
            Permission.LOW_RISK,
            object_schema(
                {"key": key, "value": {"type": "string", "minLength": 1, "maxLength": 4096}},
                ["key", "value"],
            ),
            set_preference,
        )
    )
    registry.register(
        ToolSpec(
            "personalization.preferences",
            "PERSONALIZATION",
            "Read explicitly saved E.V. preferences, separate from conversation and current desktop observations.",
            Permission.SAFE,
            object_schema({}),
            lambda a, c: {"preferences": c.daily.records("preference"), "historical": True},
            read_only=True,
        )
    )
    registry.register(
        ToolSpec(
            "personalization.reset_preference",
            "PERSONALIZATION",
            "Forget one exact explicitly selected E.V. preference without changing OS defaults or files.",
            Permission.LOW_RISK,
            object_schema({"key": key}, ["key"]),
            lambda a, c: c.daily.remove("preference", a["key"]),
        )
    )
