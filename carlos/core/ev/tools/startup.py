"""Read-only boot and desktop startup inventory. No bootloader mutations."""

import configparser
import os
import platform
from pathlib import Path

from .base import ToolSpec
from .builtin import object_schema
from ..permissions import Permission


def boot_status(_arguments, _context):
    try:
        uptime = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        uptime = None
    try:
        init = Path("/proc/1/comm").read_text().strip()
    except OSError:
        init = "unavailable"
    firmware = "UEFI" if Path("/sys/firmware/efi").is_dir() else "legacy BIOS or unavailable"
    return {
        "kernel": platform.release(),
        "firmware": firmware,
        "init_process": init,
        "uptime_seconds": uptime,
        "bootloader_modified": False,
        "message": f"Boot mode: {firmware}. Kernel: {platform.release()}. Init: {init}. Boot settings were not changed.",
    }


def startup_list(_arguments, _context):
    roots = [
        Path(item) / "autostart"
        for item in os.environ.get("XDG_CONFIG_DIRS", "/etc/xdg").split(":")
        if item
    ]
    user_root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "autostart"
    roots = list(reversed(roots)) + [user_root]
    effective = {}
    unreadable = 0
    for root in roots:
        for path in sorted(root.glob("*.desktop"))[:250]:
            try:
                if path.stat().st_size > 65536:
                    continue
                parser = configparser.ConfigParser(interpolation=None, strict=False)
                parser.read(path, encoding="utf-8")
                entry = parser["Desktop Entry"]
                effective[path.name] = {
                    "id": path.name,
                    "name": entry.get("Name", path.stem),
                    "path": str(path),
                    "enabled": entry.get("Hidden", "false").casefold() != "true",
                    "scope": "user" if root == user_root else "system",
                    "only_show_in": entry.get("OnlyShowIn", ""),
                    "not_show_in": entry.get("NotShowIn", ""),
                    "try_exec": entry.get("TryExec", ""),
                }
            except (OSError, ValueError, KeyError, configparser.Error):
                unreadable += 1
    return {
        "applications": list(effective.values()),
        "unreadable_entries": unreadable,
        "message": f"Found {len(effective)} desktop startup entries. Nothing was changed. Hidden, OnlyShowIn and TryExec conditions may affect which ones run.",
    }


def register_startup_tools(registry):
    for name, executor, description in (
        (
            "system.boot.status",
            boot_status,
            "Inspect current firmware boot mode, kernel, init and uptime without changing boot settings.",
        ),
        (
            "system.startup.list",
            startup_list,
            "Inspect effective user/system XDG desktop autostart entries, including overrides and desktop conditions, without changing launchers.",
        ),
    ):
        registry.register(
            ToolSpec(name, "SYSTEM", description, Permission.SAFE, object_schema({}), executor)
        )
