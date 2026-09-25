"""Local-first personal productivity and utility tools."""

from __future__ import annotations

import asyncio
import os
import stat
import time
from pathlib import Path

from .base import ToolRegistry, ToolSpec
from .builtin import object_schema, resolve_allowed, clipboard_read, clipboard_write
from ..permissions import Permission
from ..utilities import calculate, convert, world_clock, text_stats


def recent_files(a, c):
    path = resolve_allowed(a["path"], c)
    if not path.is_dir():
        raise ValueError("Recent files needs a directory")
    entries = []
    scanned = 0
    limited = False
    deadline = time.monotonic() + 1.0
    with os.scandir(path) as stream:
        for entry in stream:
            scanned += 1
            if scanned > 2000 or time.monotonic() > deadline:
                limited = True
                break
            if entry.name.startswith("."):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISREG(info.st_mode):
                    entries.append(
                        {
                            "id": str(path / entry.name),
                            "path": str(path / entry.name),
                            "title": entry.name,
                            "modified": info.st_mtime,
                            "bytes": info.st_size,
                            "fingerprint": f"{info.st_dev}:{info.st_ino}:{info.st_mtime_ns}:{info.st_size}",
                        }
                    )
            except OSError:
                continue
    items = sorted(entries, key=lambda item: (-item["modified"], item["path"]))[
        : a.get("limit", 10)
    ]
    message = (
        "; ".join(f"{i+1}. {item['title']}" for i, item in enumerate(items))
        or "No regular files found in that folder."
    )
    if limited:
        message += " The directory scan was limited; these may not include the newest files."
    return {
        "items": items,
        "choice_kind": "file",
        "truncated": limited,
        "path": str(path),
        "message": message,
    }


def open_selected(a, c):
    from .builtin import open_file

    path = Path(a["path"]).expanduser()
    resolved = resolve_allowed(a["path"], c)
    info = path.lstat()
    actual = f"{info.st_dev}:{info.st_ino}:{info.st_mtime_ns}:{info.st_size}"
    if not stat.S_ISREG(info.st_mode) or path.absolute() != resolved or actual != a["fingerprint"]:
        raise ValueError(
            "That file changed after the listing. List recent files again before opening it."
        )
    result = open_file({"path": str(resolved)}, c)
    return {
        **result,
        "requested": result.get("opened") is True,
        "verified": False,
        "message": f"Sent {resolved.name} to its default application. The application's resulting state is not verified.",
    }


def bookmark_open(a, c):
    from .browser import open_url

    item = c.daily.personal.read("bookmark", a["identifier"])["item"]
    if item["archived"]:
        raise ValueError("Restore that bookmark first")
    return open_url({"url": item["content"], "browser": a.get("browser", "default")}, c)


def snippet_copy(a, c):
    item = c.daily.personal.read("snippet", a["identifier"])["item"]
    if item["archived"]:
        raise ValueError("Restore that snippet first")
    write = clipboard_write({"content": item["content"]}, c)
    read = clipboard_read({}, c)
    verified = (
        write.get("ok")
        and read.get("ok")
        and not read.get("truncated")
        and read.get("content") == item["content"].rstrip("\n")
    )
    return {
        "verified": bool(verified),
        "message": (
            "Copied the snippet to your clipboard."
            if verified
            else "Could not verify the clipboard content."
        ),
    }


def clipboard_analyze(a, c):
    result = clipboard_read({}, c)
    if not result.get("ok") or result.get("truncated"):
        raise ValueError("The clipboard could not be read completely")
    return text_stats(result["content"])


async def workspace_switch(a, c):
    world = await c.desktop.snapshot(force=True)
    description = a["description"].casefold()
    desktops = world.get("desktops", [])
    if description in {"next", "previous"}:
        current = next(
            (
                i
                for i, item in enumerate(desktops)
                if str(item["id"]) == str(world.get("current_desktop"))
            ),
            None,
        )
        if current is None or not desktops:
            raise ValueError("The current virtual desktop could not be identified")
        target = desktops[(current + (1 if description == "next" else -1)) % len(desktops)]
    else:
        target = c.desktop.resolve_desktop(description, world)
    result = await c.desktop.bridge.request(
        "workspace_switch", {"desktop_id": str(target["id"])}, timeout=4
    )
    after = await c.desktop.snapshot(force=True)
    verified = str(after.get("current_desktop")) == str(target["id"])
    return {
        "verified": verified,
        "desktop": target,
        "message": (
            f"Switched to workspace {target.get('name') or target['id']}."
            if verified
            else "The workspace switch did not verify."
        ),
    }


def register_personal_tools(registry: ToolRegistry):
    text = {"type": "string", "minLength": 1, "maxLength": 160}
    content = {"type": "string", "maxLength": 8000}
    identify = object_schema({"identifier": text}, ["identifier"])

    def reg(name, description, schema, executor, permission=Permission.LOW_RISK, **kw):
        registry.register(
            ToolSpec(name, "PRODUCTIVITY", description, permission, schema, executor, **kw)
        )

    for kind, prefix in (
        ("note", "notes"),
        ("task", "tasks"),
        ("bookmark", "bookmarks"),
        ("snippet", "snippets"),
    ):
        reg(
            prefix + ".create",
            f"Explicitly save a local {kind}. Duplicate titles are rejected, never overwritten.",
            object_schema(
                {"title": text, "content": content},
                ["title"] + ([] if kind == "task" else ["content"]),
            ),
            lambda a, c, k=kind: c.daily.personal.create(k, a["title"], a.get("content", "")),
        )
        reg(
            prefix + ".list",
            f"List/search local {kind} titles with numbered immutable choices; does not read content aloud.",
            object_schema(
                {
                    "query": {"type": "string", "maxLength": 160},
                    "state": {"type": "string", "enum": ["active", "done", "archived", "all"]},
                }
            ),
            lambda a, c, k=kind: c.daily.personal.listing(
                k, a.get("query", ""), a.get("state", "active")
            ),
            Permission.SAFE,
        )
        reg(
            prefix + ".read",
            f"Read one explicitly selected saved {kind} by exact title or ID.",
            identify,
            lambda a, c, k=kind: c.daily.personal.read(k, a["identifier"]),
            Permission.SAFE,
        )
        for operation in ("archive", "restore"):
            reg(
                prefix + "." + operation,
                f"{operation.title()} one exact saved {kind}; never permanently delete its content.",
                identify,
                lambda a, c, k=kind, o=operation: c.daily.personal.change(k, a["identifier"], o),
            )
    for operation in ("complete", "reopen"):
        reg(
            "tasks." + operation,
            f"{operation.title()} one exact task and persist its state.",
            identify,
            lambda a, c, o=operation: c.daily.personal.change("task", a["identifier"], o),
        )
    reg(
        "notes.append",
        "Append text to one exact local note without replacing the existing content.",
        object_schema({"identifier": text, "content": content}, ["identifier", "content"]),
        lambda a, c: c.daily.personal.change("note", a["identifier"], "append", a["content"]),
    )
    reg(
        "bookmarks.open",
        "Open the exact saved HTTP/HTTPS bookmark. Reports URL handoff, not page load.",
        object_schema(
            {
                "identifier": text,
                "browser": {
                    "type": "string",
                    "enum": [
                        "default",
                        "firefox",
                        "chrome",
                        "chromium",
                        "brave",
                        "vivaldi",
                        "edge",
                        "opera",
                        "librewolf",
                    ],
                },
            },
            ["identifier"],
        ),
        bookmark_open,
        cancellable=False,
    )
    reg(
        "snippets.copy",
        "Copy one exact saved snippet to the clipboard and read back its content; does not paste or submit.",
        identify,
        snippet_copy,
        Permission.SENSITIVE,
    )
    reg(
        "reminders.snooze",
        "Reschedule one exact pending or fired reminder; no action routine is executed.",
        object_schema(
            {"identifier": text, "seconds": {"type": "number", "minimum": 1, "maximum": 31622400}},
            ["identifier", "seconds"],
        ),
        lambda a, c: c.daily.personal.snooze(a["identifier"], a["seconds"]),
    )
    reg(
        "utility.calculate",
        "Evaluate bounded arithmetic locally, with no code execution or network.",
        object_schema(
            {"expression": {"type": "string", "minLength": 1, "maxLength": 300}}, ["expression"]
        ),
        lambda a, c: calculate(a["expression"]),
        Permission.SAFE,
    )
    reg(
        "utility.convert",
        "Convert compatible length, mass, time, temperature, data, speed or volume units locally; not currency.",
        object_schema(
            {"value": {"type": "number"}, "source": text, "target": text},
            ["value", "source", "target"],
        ),
        lambda a, c: convert(a["value"], a["source"], a["target"]),
        Permission.SAFE,
    )
    reg(
        "utility.world_clock",
        "Read current time in a supported city or exact IANA timezone from the local timezone database.",
        identify,
        lambda a, c: world_clock(a["identifier"]),
        Permission.SAFE,
    )
    reg(
        "utility.clipboard_stats",
        "Count clipboard words, characters and lines without returning its contents or modifying it.",
        object_schema({}),
        clipboard_analyze,
        Permission.SENSITIVE,
    )
    reg(
        "files.recent",
        "List newest immediate regular files in one allowed directory; skips hidden files and symlinks, with a scan deadline.",
        object_schema(
            {
                "path": {"type": "string", "minLength": 1, "maxLength": 4096},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            ["path"],
        ),
        recent_files,
        Permission.SAFE,
    )
    reg(
        "desktop.workspace.switch",
        "Switch to an existing numbered, named, next or previous virtual desktop and verify its exact ID.",
        object_schema({"description": text}, ["description"]),
        workspace_switch,
    )
    reg(
        "files.open_selected",
        "Open one exact listed recent file only while its path, inode, size and modification time still match.",
        object_schema(
            {"path": {"type": "string", "maxLength": 4096}, "fingerprint": text},
            ["path", "fingerprint"],
        ),
        open_selected,
        cancellable=False,
    )
    reg(
        "interaction.selection_status",
        "Explain an unavailable, expired, out-of-range or incompatible numbered choice without executing anything.",
        object_schema({}),
        lambda a, c: {
            "verified": False,
            "message": "List the relevant notes, tasks, bookmarks, snippets, recent files or screen controls first, then choose a valid number within two minutes. Nothing was changed.",
        },
        Permission.SAFE,
    )
