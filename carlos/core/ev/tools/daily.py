from __future__ import annotations

from ev.platform import executable as _platform_executable

from .base import ToolRegistry, ToolSpec, ToolContext
from .builtin import object_schema, run_command
from ..permissions import Permission
import asyncio
import json
import re
from pathlib import Path


async def power_request(arguments, context):
    return await context.power.request(arguments["action"])


async def power_cancel(_arguments, context):
    return await context.power.cancel()


def show_desktop(arguments, _context):
    requested = arguments["show"]
    result = run_command(
        [
            _platform_executable("/usr/bin/qdbus6"),
            "org.kde.KWin",
            "/KWin",
            "org.kde.KWin.showDesktop",
            str(requested).lower(),
        ],
        timeout=4,
    )
    actual = run_command(
        [
            _platform_executable("/usr/bin/qdbus6"),
            "org.kde.KWin",
            "/KWin",
            "org.kde.KWin.showingDesktop",
        ],
        timeout=4,
    )
    return {
        "verified": result["ok"]
        and actual["ok"]
        and actual["stdout"].strip() == str(requested).lower(),
        "message": "Desktop shown." if requested else "Windows restored.",
    }


def scene_catalog():
    root = Path.home()
    live_file = root / ".local/share/phaxity-live-wallpapers/live-scenes.json"
    catalog = []
    if live_file.is_file():
        for key, item in json.loads(live_file.read_text()).items():
            if re.fullmatch(r"[a-z][a-z0-9-]{0,80}", key):
                catalog.append(
                    {
                        "id": key,
                        "name": item["name"],
                        "live": True,
                        "preview": Path(item["fallback_path"]).as_uri(),
                        "accent": item["accent"],
                        "background": item["background"],
                        "secondary": item.get("hover", item["accent"]),
                    }
                )
    static_file = root / ".local/share/phaxity-scenes/spectrum-scenes.json"
    if static_file.is_file():
        for key, item in json.loads(static_file.read_text()).items():
            if re.fullmatch(r"[a-z][a-z0-9-]{0,80}", key):
                image = root / ".Pictures/Wallpapers" / item["wallpaper"]
                catalog.append(
                    {
                        "id": key,
                        "name": item.get("name", key.title()),
                        "live": False,
                        "preview": image.as_uri(),
                        "accent": item["accent"],
                        "background": item["background"],
                        "secondary": item.get("hover", item["accent"]),
                    }
                )
    for key, name, filename, accent, background in (
        ("dark", "Midnight Terminal", "Darky.jpg", "#58d680", "#050806"),
        ("light", "Sakura Glass", "Iloveu.jpg", "#655286", "#f8edf3"),
    ):
        image = root / ".Pictures/Wallpapers" / filename
        if image.is_file():
            catalog.append(
                {
                    "id": key,
                    "name": name,
                    "live": False,
                    "preview": image.as_uri(),
                    "accent": accent,
                    "background": background,
                    "secondary": accent,
                }
            )
    return catalog


def scenes_list(_arguments, _context):
    scenes = scene_catalog()
    return {
        "scenes": scenes,
        "message": "Available scenes: " + ", ".join(item["name"] for item in scenes),
    }


def scenes_apply(arguments, _context):
    query = arguments["name"].strip().casefold()
    matches = [
        item
        for item in scene_catalog()
        if query in {item["id"].casefold(), item["name"].casefold()}
    ]
    if len(matches) != 1:
        return {"verified": False, "message": "Specify one scene name from the wallpaper picker."}
    item = matches[0]
    script = (
        Path.home() / ".local/bin" / ("phaxity-live-scene" if item["live"] else "phaxity-scene")
    )
    result = run_command([str(script), item["id"]], timeout=60)
    status = run_command([str(script), "status"], timeout=8)
    verified = result["ok"] and status["ok"] and status["stdout"].strip() == item["id"]
    return {
        "verified": verified,
        "scene": item,
        "message": (
            f"Applied {item['name']} and its matching colors."
            if verified
            else "The scene switch did not verify; inspect the scene manager before trying again."
        ),
    }


async def spotify_search(a, c):
    return await c.spotify.search(a["query"], a.get("kind", "track"))


async def spotify_play(a, c):
    return await c.spotify.play(
        a["query"],
        a.get("kind", "track"),
        own_playlist=a.get("own_playlist", False),
        random=a.get("random", False),
    )


async def spotify_playlists(a, c):
    return await c.spotify.playlists()


async def spotify_diagnose(a, c):
    return await c.spotify.diagnose()


def register_daily_tools(registry: ToolRegistry) -> None:
    from .personal import register_personal_tools
    from .settings import register_settings_tools

    register_personal_tools(registry)
    register_settings_tools(registry)
    from .controls import register_controls_tools

    register_controls_tools(registry)
    from .browser import register_browser_tools

    register_browser_tools(registry)
    from .startup import register_startup_tools

    register_startup_tools(registry)
    text = {"type": "string", "minLength": 1, "maxLength": 500}
    name = {"type": "string", "minLength": 1, "maxLength": 100}
    empty = object_schema({})

    def register(tool, category, description, permission, schema, executor, **kwargs):
        registry.register(
            ToolSpec(tool, category, description, permission, schema, executor, **kwargs)
        )

    register(
        "system.power",
        "SYSTEM",
        "Schedule an explicit user-requested shutdown, reboot, logout, suspend, or lock using native KDE/login1. Never force past unsaved-work dialogs.",
        Permission.HIGH,
        object_schema(
            {
                "action": {
                    "type": "string",
                    "enum": ["shutdown", "reboot", "logout", "lock", "suspend"],
                }
            },
            ["action"],
        ),
        power_request,
    )
    register(
        "system.power.cancel",
        "SYSTEM",
        "Cancel a pending E.V. power countdown before dispatch.",
        Permission.SAFE,
        empty,
        power_cancel,
    )
    register(
        "system.power.status",
        "SYSTEM",
        "Read any pending power countdown without changing the session.",
        Permission.SAFE,
        empty,
        lambda a, c: {"pending": dict(c.power.pending)},
    )
    register(
        "security.firewall.runtime",
        "SECURITY",
        "Read active kernel firewall tables, chains, policies and rule counts. Optional OS administrator dialog grants only this read; no rule edits.",
        Permission.SENSITIVE,
        object_schema({"authorize": {"type": "boolean"}}),
        lambda a, c: c.security_center.firewall_runtime(a.get("authorize", False)),
        timeout_seconds=105,
    )
    register(
        "desktop.show_desktop",
        "DESKTOP",
        "Show the desktop or restore its windows and verify KWin's state.",
        Permission.LOW_RISK,
        object_schema({"show": {"type": "boolean"}}, ["show"]),
        show_desktop,
    )
    register(
        "reminders.create",
        "REMINDERS",
        "Persist a timer/reminder or recurring reminder. Delivers inside E.V.; never executes a shell command.",
        Permission.LOW_RISK,
        object_schema(
            {
                "label": text,
                "seconds": {"type": "number", "minimum": 1, "maximum": 31622400},
                "repeat_seconds": {"type": "number", "minimum": 0, "maximum": 31622400},
            },
            ["label", "seconds"],
        ),
        lambda a, c: c.daily.add_reminder(a["label"], a["seconds"], a.get("repeat_seconds", 0)),
    )
    register(
        "reminders.list",
        "REMINDERS",
        "List pending timers, alarms and reminders with deadlines.",
        Permission.SAFE,
        empty,
        lambda a, c: {"reminders": c.daily.reminders()},
    )
    register(
        "reminders.cancel",
        "REMINDERS",
        "Cancel one exact reminder by ID or unique label.",
        Permission.LOW_RISK,
        object_schema({"identifier": text}, ["identifier"]),
        lambda a, c: c.daily.cancel_reminder(a["identifier"]),
    )
    for kind, prefix in (("alias", "applications.alias"), ("routine", "routines")):
        register(
            prefix + ".list",
            "PERSONALIZATION",
            f"List explicitly saved {kind}s.",
            Permission.SAFE,
            empty,
            lambda a, c, k=kind: {"items": c.daily.records(k)},
        )
        register(
            prefix + ".remove",
            "PERSONALIZATION",
            f"Delete one saved {kind}.",
            Permission.LOW_RISK,
            object_schema({"name": name}, ["name"]),
            lambda a, c, k=kind: c.daily.remove(k, a["name"]),
        )
    register(
        "applications.alias.save",
        "PERSONALIZATION",
        "Save or update a user-chosen app nickname. It maps to an app name, never executable code.",
        Permission.LOW_RISK,
        object_schema({"name": name, "target": text}, ["name", "target"]),
        lambda a, c: c.daily.save("alias", a["name"], a["target"]),
    )
    register(
        "routines.save",
        "PERSONALIZATION",
        "Save or edit a named sequence of explicit desktop requests for later use; this does not execute them.",
        Permission.LOW_RISK,
        object_schema(
            {"name": name, "commands": {"type": "array", "maxItems": 8, "items": text}},
            ["name", "commands"],
        ),
        lambda a, c: c.daily.save("routine", a["name"], a["commands"]),
    )
    register(
        "scenes.list",
        "SCENES",
        "List existing wallpaper scenes with local previews and associated color palettes.",
        Permission.SAFE,
        empty,
        scenes_list,
    )
    register(
        "scenes.apply",
        "SCENES",
        "Apply one exact existing Phaxity scene and its color theme through the user's scene manager.",
        Permission.LOW_RISK,
        object_schema({"name": name}, ["name"]),
        scenes_apply,
        timeout_seconds=75,
    )
    register(
        "spotify.status",
        "SPOTIFY",
        "Check whether private saved Spotify credentials are available without returning any secrets.",
        Permission.SAFE,
        empty,
        lambda a, c: c.spotify.status(),
    )
    search_schema = object_schema(
        {
            "query": text,
            "kind": {"type": "string", "enum": ["track", "album", "artist", "playlist"]},
        },
        ["query"],
    )
    register(
        "spotify.search",
        "SPOTIFY",
        "Search Spotify for tracks, artists, albums or playlists using the saved authorized connection.",
        Permission.SAFE,
        search_schema,
        spotify_search,
    )
    play_schema = object_schema(
        {
            **search_schema["properties"],
            "own_playlist": {"type": "boolean"},
            "random": {"type": "boolean"},
        },
        ["query"],
    )
    register(
        "spotify.play",
        "SPOTIFY",
        "Search and start a NEW explicitly named song, album or playlist, or exact Spotify URI; optionally choose a random track from an exact saved playlist. Replaces the current selection and requires Spotify account/API eligibility. To resume the existing track without changing the queue use audio.media with action play and player spotify instead.",
        Permission.LOW_RISK,
        play_schema,
        spotify_play,
        timeout_seconds=50,
    )
    register(
        "spotify.diagnose",
        "SPOTIFY",
        "Check the saved connection with read-only Spotify requests; never return tokens or start playback.",
        Permission.SAFE,
        empty,
        spotify_diagnose,
        timeout_seconds=50,
    )
    register(
        "spotify.playlists",
        "SPOTIFY",
        "List the user's first 30 Spotify playlists using saved authorization.",
        Permission.SAFE,
        empty,
        spotify_playlists,
    )
