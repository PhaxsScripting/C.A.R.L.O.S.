"""Explicit, private window-layout snapshots and fresh typed restoration plans.

No guessed browser tabs, shell commands, implicit app launches or file contents.
Restoration is deliberately partial across app/compositor restarts: report gaps.
"""

import asyncio
import json
import time
from pathlib import Path

import psutil

from ..permissions import Permission
from .base import ToolSpec, ValidationError
from .builtin import object_schema, resolve_allowed, desktop_entries, open_application


def _process_start(pid):
    return psutil.Process(int(pid)).create_time()


def _boot_id():
    from ev.platform import IS_FREEBSD

    if IS_FREEBSD:
        from ev.platform.system import boot_id

        return boot_id()
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def _topology(world):
    return [
        {"name": o["name"], "geometry": o["geometry"]}
        for o in world.get("outputs", [])
        if o.get("enabled", True)
    ]


def _load(context, name):
    record = context.daily.records("workspace_layout").get(name.strip().casefold())
    if not record:
        raise ValidationError("No workspace layout with that exact saved name")
    return record


def _insert(context, name, record):
    with context.daily.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if (
            db.execute("SELECT count(*) FROM records WHERE kind='workspace_layout'").fetchone()[0]
            >= 50
        ):
            raise ValidationError(
                "Workspace snapshot limit reached (50); remove an old saved snapshot first"
            )
        if db.execute(
            "SELECT 1 FROM records WHERE kind='workspace_layout' AND name=?", (name,)
        ).fetchone():
            raise ValidationError(
                "That snapshot name already exists; choose a new name to preserve the original"
            )
        db.execute(
            "INSERT INTO records VALUES ('workspace_layout',?,?)", (name, json.dumps(record))
        )


def _capture_context(arguments, context):
    supplied = arguments.get("context", {})
    result = {}
    for key in ("project", "terminal_cwds", "editor_files"):
        if key not in supplied:
            continue
        values = [supplied[key]] if key == "project" else supplied[key]
        checked = []
        for value in values:
            path = resolve_allowed(value, context)
            if key == "editor_files" and not path.is_file():
                raise ValidationError("Editor references must be existing files")
            if key != "editor_files" and not path.is_dir():
                raise ValidationError("Project and terminal CWDs must be existing directories")
            checked.append(str(path))
        result[key] = checked[0] if key == "project" else checked
    for key in ("codex_session", "task_id", "hud_layout"):
        if key in supplied:
            result[key] = supplied[key]
    return result


def _desktop_id(window, entries):
    app = str(window.get("app_id", "")).casefold().removesuffix(".desktop")
    wmclass = str(window.get("resource_class", "")).casefold()
    matches = [
        key
        for key, entry in entries.items()
        if (app and key.casefold() == app)
        or (wmclass and str(entry.get("_startup_wm_class", "")).casefold() == wmclass)
    ]
    return matches[0] if len(matches) == 1 else None


async def capture(arguments, context):
    saved_context = await asyncio.to_thread(_capture_context, arguments, context)
    world = await context.desktop.snapshot(force=True)
    identifiers = arguments["window_ids"]
    if len(set(identifiers)) != len(identifiers):
        raise ValidationError("Choose distinct exact window IDs")
    records = []
    entries = await asyncio.to_thread(desktop_entries)
    for identifier in identifiers:
        matches = [
            w
            for w in world.get("windows", [])
            if w.get("id") == identifier and w.get("normal") and not w.get("special")
        ]
        if len(matches) != 1:
            raise ValidationError(
                "Every selected window must be a current normal application window"
            )
        window = matches[0]
        saved = {
            key: window.get(key)
            for key in (
                "id",
                "pid",
                "app_id",
                "resource_class",
                "geometry",
                "output",
                "minimized",
                "fullscreen",
                "maximized",
                "desktops",
            )
        }
        saved["desktop_id"] = _desktop_id(window, entries)
        saved["process_start"] = await asyncio.to_thread(_process_start, window["pid"])
        records.append(saved)
    name = arguments["name"].strip().casefold()
    record = {
        "version": 2,
        "context": saved_context,
        "created": time.time(),
        "boot_id": _boot_id(),
        "windows": records,
        "outputs": _topology(world),
        "active_window_id": (
            world.get("active_window_id") if world.get("active_window_id") in identifiers else None
        ),
    }
    await asyncio.to_thread(_insert, context, name, record)
    return {
        "verified": (await asyncio.to_thread(_load, context, name)) == record,
        "name": name,
        "windows_saved": len(records),
        "context_saved": list(saved_context),
        "scope": "Selected window metadata and explicit context references; no file contents, shell history, unsaved buffers or screenshots",
    }


def _same_window(saved, current):
    return all(saved.get(k) == current.get(k) for k in ("id", "pid", "app_id", "resource_class"))


async def resolve_saved(arguments, context):
    record = await asyncio.to_thread(_load, context, arguments["name"])
    world = await context.desktop.snapshot(force=True)
    if record["boot_id"] != _boot_id() or record["outputs"] != _topology(world):
        raise ValidationError(
            "Boot or monitor topology changed; old geometry must be replanned rather than replayed"
        )
    saved = next((w for w in record["windows"] if w["id"] == arguments["window_id"]), None)
    current = next((w for w in world.get("windows", []) if saved and _same_window(saved, w)), None)
    if not saved or not current or not current.get("normal") or current.get("special"):
        raise ValidationError(
            "Saved exact application window no longer exists; no substitute was selected"
        )
    if await asyncio.to_thread(_process_start, current["pid"]) != saved["process_start"]:
        raise ValidationError("Process identity changed since the snapshot")
    return {"window": current, "saved": saved, "resolved": True}


async def relaunch_saved(arguments, context):
    record = await asyncio.to_thread(_load, context, arguments["name"])
    saved = next((w for w in record["windows"] if w["id"] == arguments["window_id"]), None)
    if not saved or not saved.get("desktop_id"):
        raise ValidationError("Saved window has no unambiguous installed application identity")
    entries = await asyncio.to_thread(desktop_entries)
    if _desktop_id(saved, entries) != saved["desktop_id"]:
        raise ValidationError("Installed application identity changed")
    world = await context.desktop.snapshot(force=True)
    if record["outputs"] != _topology(world):
        raise ValidationError("Monitor topology changed; relaunch layout requires replanning")

    def matches(window):
        return (
            window.get("normal")
            and not window.get("special")
            and window.get("app_id") == saved.get("app_id")
            and window.get("resource_class") == saved.get("resource_class")
        )

    if any(matches(window) for window in world.get("windows", [])):
        raise ValidationError(
            "A matching application window already exists; no substitute or duplicate was selected"
        )
    existing = {w["id"] for w in world.get("windows", [])}
    launched = await asyncio.to_thread(
        open_application, {"desktop_id": saved["desktop_id"]}, context
    )
    if not launched.get("launched"):
        raise ValidationError("Application launch was not acknowledged; no retry issued")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        fresh = await context.desktop.snapshot(force=True)
        windows = [w for w in fresh.get("windows", []) if w["id"] not in existing and matches(w)]
        if len(windows) > 1:
            raise ValidationError(
                "Application opened multiple matching windows; no geometry was changed"
            )
        if len(windows) == 1:
            return {
                "window": windows[0],
                "saved": saved,
                "relaunched": True,
                "verified": True,
                "scope": "Fresh application window observed; tabs and unsaved contents were not restored",
            }
        await asyncio.sleep(0.2)
    raise ValidationError("No unique application window appeared; no retry issued")


async def restore_plan(arguments, context):
    record = await asyncio.to_thread(_load, context, arguments["name"])
    steps, conditions, gaps = [], [], []
    world = await context.desktop.snapshot(force=True)
    desktops = {d["id"] for d in world.get("desktops", [])}
    for index, saved in enumerate(record["windows"]):
        resolver = "workspaces.resolve_window"
        try:
            await resolve_saved({"name": arguments["name"], "window_id": saved["id"]}, context)
        except (ValueError, RuntimeError, OSError, psutil.Error) as error:
            same_app = [
                w
                for w in world.get("windows", [])
                if w.get("app_id") == saved.get("app_id")
                and w.get("resource_class") == saved.get("resource_class")
            ]
            saved_count = sum(
                w.get("desktop_id") == saved.get("desktop_id") for w in record["windows"]
            )
            if (
                saved.get("desktop_id")
                and saved_count == 1
                and not same_app
                and record["outputs"] == _topology(world)
            ):
                resolver = "workspaces.relaunch_window"
            else:
                gaps.append(
                    {"window_id": saved["id"], "app_id": saved.get("app_id"), "reason": str(error)}
                )
                continue
        identifier = f"window{index}"
        target = {"$ref": f"{identifier}.result.window.id"}
        steps.append(
            {
                "id": identifier,
                "tool": resolver,
                "arguments": {"name": arguments["name"], "window_id": saved["id"]},
            }
        )
        saved_desktops = saved.get("desktops") or []
        if len(saved_desktops) == 1 and saved_desktops[0] in desktops:
            steps.append(
                {
                    "id": f"desktop{index}",
                    "tool": "desktop.window.move_to_workspace",
                    "arguments": {"window_id": target, "desktop_id": saved_desktops[0]},
                }
            )
        elif saved_desktops:
            gaps.append(
                {
                    "window_id": saved["id"],
                    "reason": "Saved virtual desktop assignment cannot be restored exactly",
                }
            )
        # Geometry alone does not leave fullscreen or unminimize a window.
        # Restore explicitly before applying the saved rectangle/mode.
        steps.append(
            {
                "id": f"restore{index}",
                "tool": "desktop.window.state",
                "arguments": {"window_id": target, "state": "restore"},
            }
        )
        steps.append(
            {
                "id": f"geometry{index}",
                "tool": "desktop.window.move_resize",
                "arguments": {"window_id": target, **saved["geometry"]},
            }
        )
        mode = (
            "fullscreen"
            if saved.get("fullscreen")
            else "maximize" if saved.get("maximized") else None
        )
        if mode:
            steps.append(
                {
                    "id": f"mode{index}",
                    "tool": "desktop.window.state",
                    "arguments": {"window_id": target, "state": mode},
                }
            )
            conditions.append(
                {
                    "kind": "window_state",
                    "window_id": target,
                    "property": "fullscreen" if mode == "fullscreen" else "maximized",
                    "expected": True,
                }
            )
        else:
            conditions.append(
                {"kind": "window_geometry", "window_id": target, "geometry": saved["geometry"]}
            )
        if saved.get("minimized"):
            steps.append(
                {
                    "id": f"minimize{index}",
                    "tool": "desktop.window.state",
                    "arguments": {"window_id": target, "state": "minimize"},
                }
            )
        conditions.append(
            {
                "kind": "window_state",
                "window_id": target,
                "property": "minimized",
                "expected": bool(saved.get("minimized")),
            }
        )
    saved_context = record.get("context", {})
    checks = []
    for kind in ("project", "terminal_cwds", "editor_files"):
        values = (
            [saved_context[kind]]
            if kind == "project" and kind in saved_context
            else saved_context.get(kind, [])
        )
        for value in values:
            try:
                path = resolve_allowed(value, context)
                available = path.is_file() if kind == "editor_files" else path.is_dir()
            except (ValueError, OSError):
                available = False
            checks.append({"kind": kind, "path": value, "available": available, "restored": False})
    return {
        "name": arguments["name"],
        "context": saved_context,
        "context_checks": checks,
        "changed": False,
        "all_saved_windows_resolvable": not gaps,
        "exact_session_restoration": False,
        "gaps": gaps,
        "plan": (
            {
                "goal": f"Restore the surviving windows of layout {arguments['name']}",
                "steps": steps,
                "conditions": conditions,
            }
            if steps
            else None
        ),
        "instructions": "Preview only. Execute the returned typed plan using agent.execute_plan if requested. Every target is rechecked at execution. A uniquely identified missing application may be relaunched and its fresh window discovered. Ambiguous applications are reported as gaps. Existing single virtual-desktop assignments are restored when resolvable. Context references are available for explicit reopening; unsaved editor buffers, browser tabs, terminal commands, focus and stacking order are not replayed.",
    }


async def capture_current(arguments, context):
    world = await context.desktop.snapshot(force=True)
    windows = [
        w["id"] for w in world.get("windows", []) if w.get("normal") and not w.get("special")
    ]
    if not 1 <= len(windows) <= 5:
        raise ValidationError(
            "Automatic setup capture needs 1-5 normal windows; select exact windows for a larger desktop"
        )
    return await capture({"name": arguments["name"], "window_ids": windows}, context)


def register_workspace_tools(registry):
    name = {
        "type": "string",
        "minLength": 1,
        "maxLength": 100,
        "pattern": "[A-Za-z0-9][A-Za-z0-9 _.-]*",
    }
    identifier = {"type": "string", "minLength": 1, "maxLength": 100}

    async def listing(a, c):
        saved = await asyncio.to_thread(c.daily.records, "workspace_layout")
        return {
            "workspaces": [
                {"name": n, "created": r["created"], "windows": len(r["windows"])}
                for n, r in saved.items()
            ],
            "historical": True,
        }

    context_schema = object_schema(
        {
            "project": {"type": "string", "minLength": 1, "maxLength": 4096},
            "terminal_cwds": {
                "type": "array",
                "maxItems": 10,
                "items": {"type": "string", "minLength": 1, "maxLength": 4096},
            },
            "editor_files": {
                "type": "array",
                "maxItems": 10,
                "items": {"type": "string", "minLength": 1, "maxLength": 4096},
            },
            "codex_session": {"type": "string", "maxLength": 100},
            "task_id": {"type": "string", "maxLength": 100},
            "hud_layout": {
                "type": "string",
                "enum": ["CARLOS", "PROJECT", "SYSTEM", "MEDIA", "REMOTE"],
            },
        },
        [],
    )
    registry.register(
        ToolSpec(
            "workspaces.capture",
            "WORKSPACES",
            "Explicitly save the layout of 1-5 exact selected application windows under a new name. No title/content/tab collection, app launch, layout change or overwrite. Preserves process and monitor identity for safe future restoration.",
            Permission.LOW_RISK,
            object_schema(
                {
                    "name": name,
                    "context": context_schema,
                    "window_ids": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 5,
                        "items": identifier,
                    },
                },
                ["name", "window_ids"],
            ),
            capture,
        )
    )
    registry.register(
        ToolSpec(
            "workspaces.capture_current",
            "WORKSPACES",
            "Save the current 1-5 normal application windows under an explicit new name. Reject larger desktops instead of silently dropping windows.",
            Permission.LOW_RISK,
            object_schema({"name": name}, ["name"]),
            capture_current,
            offline_available=True,
            reversible=True,
        )
    )
    registry.register(
        ToolSpec(
            "workspaces.list",
            "WORKSPACES",
            "List explicitly saved local window-layout snapshots; historical metadata, not current application state.",
            Permission.SAFE,
            object_schema({}, []),
            listing,
            read_only=True,
        )
    )
    registry.register(
        ToolSpec(
            "workspaces.remove",
            "WORKSPACES",
            "Remove one exact saved layout's metadata only when requested. Does not close apps, change windows or delete project files.",
            Permission.LOW_RISK,
            object_schema({"name": name}, ["name"]),
            lambda a, c: c.daily.remove("workspace_layout", a["name"]),
        )
    )
    registry.register(
        ToolSpec(
            "workspaces.restore_plan",
            "WORKSPACES",
            "Generate a typed restoration plan from a saved layout after fresh window/process/monitor checks. Reports missing windows and topology changes, never substitutes an app or falsely promises exact restoration. No actions until agent.execute_plan executes the returned plan.",
            Permission.SAFE,
            object_schema({"name": name}, ["name"]),
            restore_plan,
            read_only=True,
        )
    )
    registry.register(
        ToolSpec(
            "workspaces.relaunch_window",
            "WORKSPACES",
            "Relaunch one missing saved application using its exact installed desktop identity; require a unique fresh window. Does not restore tabs, buffers or shell commands.",
            Permission.LOW_RISK,
            object_schema({"name": name, "window_id": identifier}, ["name", "window_id"]),
            relaunch_saved,
            offline_available=True,
            reversible=False,
            timeout_seconds=15,
            side_effects=("Launches one explicitly saved application",),
        )
    )
    registry.register(
        ToolSpec(
            "workspaces.resolve_window",
            "WORKSPACES",
            "Revalidate one exact saved window against fresh KWin state, boot ID, process start time and monitor topology before restoring it.",
            Permission.SAFE,
            object_schema({"name": name, "window_id": identifier}, ["name", "window_id"]),
            resolve_saved,
            read_only=True,
        )
    )
