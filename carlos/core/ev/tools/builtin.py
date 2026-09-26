from __future__ import annotations

from ev.platform import executable as _platform_executable

import asyncio
import configparser
import hashlib
import json
import os
import platform
import re
import shlex
import signal
import shutil
import subprocess
import time
import threading
from copy import deepcopy
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import psutil

from ..permissions import Permission
from ..telemetry import read_temperature
from .base import ToolContext, ToolRegistry, ToolSpec, ValidationError
from .media import control_media

EMPTY_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}
PROTECTED_PROCESSES = {"init", "systemd", "plasmashell", "kwin_wayland", "dbus-daemon", "ev-core"}
_SCRIPT_INTERPRETER = re.compile(
    r"^(?:python(?:\d+(?:\.\d+)*)?|pypy\d*|node(?:js)?|ruby|perl|lua(?:\d+(?:\.\d+)*)?)$",
    re.IGNORECASE,
)
_SPOTIFY_MPRIS_SERVICE = "org.mpris.MediaPlayer2.spotify"
_SPOTIFY_FLATPAK_ID = "com.spotify.Client"
_DESKTOP_ENTRY_CACHE: dict[str, dict[str, Any]] = {}
_DESKTOP_ENTRY_CACHE_KEY: tuple[str, ...] = ()
_DESKTOP_ENTRY_CACHE_AT = 0.0
_PROCESS_KEY_CACHE: dict[str, set[int]] = {}
_PROCESS_KEY_CACHE_AT = 0.0
_OUTPUT_CONTROL_LOCK = threading.Lock()


def object_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def _identity_tokens(value: str) -> set[str]:
    """Tokenize a private process identity without returning its command line."""

    camel_spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)
    return set(re.findall(r"[a-z0-9]+", camel_spaced.casefold()))


def _script_argv_identity(cmdline: list[str]) -> list[str]:
    """Return only the launched script/module identity for known interpreters."""

    if not cmdline:
        return []
    launcher = Path(str(cmdline[0])).name
    if not _SCRIPT_INTERPRETER.fullmatch(launcher):
        return []
    arguments = [str(item) for item in cmdline[1:]]
    for index, argument in enumerate(arguments):
        if argument == "-m" and index + 1 < len(arguments):
            return [arguments[index + 1]]
        if argument in {"-c", "--command"}:
            return []
        if not argument.startswith("-"):
            return [Path(argument).name]
    return []


def _private_process_identity_keys(info: dict[str, Any]) -> set[str]:
    cmdline = [str(item) for item in (info.get("cmdline") or [])]
    values = [
        str(info.get("name") or ""),
        Path(str(info.get("exe") or "")).name,
        Path(cmdline[0]).name if cmdline else "",
        *_script_argv_identity(cmdline),
    ]
    return {_application_key(value) for value in values if _application_key(value)}


def _private_process_identity(info: dict[str, Any]) -> set[str]:
    """Build matching tokens from metadata that must never leave the matcher."""

    tokens: set[str] = set()
    for key in _private_process_identity_keys(info):
        tokens.update(key.split())
    return tokens


def _process_query_token_sets(query: str) -> list[set[str]]:
    candidates = [_identity_tokens(query)]
    try:
        scored = [
            (score, entry)
            for entry in desktop_entries().values()
            if (score := _application_match(entry, query)[0]) >= 84
        ]
    except NameError:
        scored = []
    if scored:
        best = max(score for score, _entry in scored)
        matches = [entry for score, entry in scored if score == best]
        if len(matches) == 1:
            candidates.extend(
                _identity_tokens(identity) for identity in _entry_identity_keys(matches[0])
            )
    unique: list[set[str]] = []
    for candidate in candidates:
        if candidate and candidate not in unique:
            unique.append(candidate)
    return unique


def _process_matches_query(
    info: dict[str, Any],
    query: str,
    identity_sets: list[set[str]] | None = None,
) -> bool:
    process_keys = _private_process_identity_keys(info)
    candidates = identity_sets if identity_sets is not None else _process_query_token_sets(query)
    for candidate in candidates:
        if len(candidate) == 1:
            if " ".join(candidate) in process_keys:
                return True
        elif any(candidate.issubset(set(key.split())) for key in process_keys):
            return True
    # The display name might not match the process name. Find one clear
    # desktop entry, then compare its launch and window identities.
    # Keep process arguments private.
    return False


def run_command(
    arguments: list[str], timeout: float = 15, cwd: Path | None = None, max_output: int = 131_072
) -> dict[str, Any]:
    started = time.perf_counter()
    result = subprocess.run(
        arguments,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
        env={**os.environ, "LC_ALL": "C"},
    )
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": result.stdout[:max_output],
        "stderr": result.stderr[:max_output],
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        "truncated": len(result.stdout) > max_output or len(result.stderr) > max_output,
    }


def allowed_roots(context: ToolContext) -> list[Path]:
    roots = []
    for configured in context.config["security"]["allowed_roots"]:
        path = Path(configured).expanduser()
        try:
            roots.append(path.resolve(strict=True))
        except FileNotFoundError:
            continue
    return roots


def resolve_allowed(path_text: str, context: ToolContext, must_exist: bool = True) -> Path:
    path = Path(path_text).expanduser()
    try:
        resolved = path.resolve(strict=must_exist)
    except (FileNotFoundError, RuntimeError) as error:
        raise ValidationError(f"path does not exist: {path}") from error
    if not any(
        resolved == root or resolved.is_relative_to(root) for root in allowed_roots(context)
    ):
        raise ValidationError(f"path is outside E.V. allowed roots: {resolved}")
    return resolved


def resolve_destination(path_text: str, context: ToolContext) -> Path:
    """Resolve a not-yet-created path while validating every existing parent."""

    destination = Path(path_text).expanduser().resolve(strict=False)
    if not any(
        destination == root or destination.is_relative_to(root) for root in allowed_roots(context)
    ):
        raise ValidationError(f"path is outside E.V. allowed roots: {destination}")
    ancestor = destination.parent
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    try:
        resolved_ancestor = ancestor.resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as error:
        raise ValidationError("destination has no valid existing parent") from error
    if not any(
        resolved_ancestor == root or resolved_ancestor.is_relative_to(root)
        for root in allowed_roots(context)
    ):
        raise ValidationError("destination parent escapes E.V. allowed roots")
    return destination


def normalize_path_argument(field: str):
    def normalize(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        normalized = dict(arguments)
        normalized[field] = str(resolve_allowed(str(arguments[field]), context))
        return normalized

    return normalize


def get_cpu_usage(_arguments: dict[str, Any], _context: ToolContext) -> dict[str, Any]:
    frequency = psutil.cpu_freq()
    return {
        "percent": round(psutil.cpu_percent(interval=0.15), 1),
        "per_cpu_percent": [
            round(value, 1) for value in psutil.cpu_percent(interval=None, percpu=True)
        ],
        "load_average": [round(value, 2) for value in os.getloadavg()],
        "logical_cpus": psutil.cpu_count(),
        "physical_cpus": psutil.cpu_count(logical=False),
        "frequency_mhz": None if frequency is None else round(frequency.current),
    }


def get_memory_usage(_arguments: dict[str, Any], _context: ToolContext) -> dict[str, Any]:
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    return {
        "memory": {
            "total_bytes": memory.total,
            "available_bytes": memory.available,
            "used_bytes": memory.used,
            "percent": memory.percent,
        },
        "swap": {
            "total_bytes": swap.total,
            "used_bytes": swap.used,
            "free_bytes": swap.free,
            "percent": swap.percent,
        },
    }


def get_temperature(_arguments: dict[str, Any], _context: ToolContext) -> dict[str, Any]:
    return read_temperature()


def get_disk_usage(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    requested = arguments.get("path", str(Path.home()))
    if requested == "/":
        path = Path("/")
    else:
        path = resolve_allowed(requested, context)
    usage = psutil.disk_usage(path)
    return {
        "path": str(path),
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "percent": usage.percent,
    }


def get_processes(arguments: dict[str, Any], _context: ToolContext) -> dict[str, Any]:
    limit = int(arguments.get("limit", 15))
    sort_key = arguments.get("sort", "memory")
    query = arguments.get("query", "").strip()
    query_tokens = _identity_tokens(query)
    query_identities = _process_query_token_sets(query) if query_tokens else []
    attributes = ["pid", "name", "username", "memory_info", "create_time"]
    if query_tokens:
        attributes.extend(["exe", "cmdline"])
    processes = list(psutil.process_iter(attributes))
    for process in processes:
        try:
            process.cpu_percent(interval=None)
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass
    time.sleep(0.15)
    rows = []
    for process in processes:
        try:
            info = process.info
            name = info["name"] or "unknown"
            if query_tokens and not _process_matches_query(info, query, query_identities):
                continue
            rows.append(
                {
                    "pid": info["pid"],
                    "ppid": process.ppid(),
                    "name": name,
                    "username": info["username"],
                    "rss_bytes": info["memory_info"].rss if info["memory_info"] else 0,
                    "cpu_percent": round(process.cpu_percent(interval=None), 2),
                    "started_at_epoch": round(info["create_time"] or 0.0, 3),
                }
            )
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    key = "cpu_percent" if sort_key == "cpu" else "rss_bytes"
    rows.sort(key=lambda row: row[key], reverse=True)
    return {"processes": rows[:limit], "inspected": len(rows), "sort": sort_key}


def get_network_status(_arguments: dict[str, Any], _context: ToolContext) -> dict[str, Any]:
    interfaces = []
    addresses = psutil.net_if_addrs()
    for name, stats in psutil.net_if_stats().items():
        interfaces.append(
            {
                "name": name,
                "up": stats.isup,
                "speed_mbps": stats.speed,
                "addresses": [
                    address.address
                    for address in addresses.get(name, [])
                    if address.family.name in {"AF_INET", "AF_INET6"}
                ],
            }
        )
    counters = psutil.net_io_counters()
    return {
        "interfaces": interfaces,
        "bytes_sent": counters.bytes_sent,
        "bytes_received": counters.bytes_recv,
    }


def get_battery(_arguments: dict[str, Any], _context: ToolContext) -> dict[str, Any]:
    battery = psutil.sensors_battery()
    if battery is None:
        return {"present": False}
    return {
        "present": True,
        "percent": battery.percent,
        "plugged": battery.power_plugged,
        "seconds_left": battery.secsleft,
    }


def _application_key(value: str) -> str:
    camel_spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)
    return " ".join(re.findall(r"[a-z0-9]+", camel_spaced.casefold()))


def _exec_identities(command: str) -> list[str]:
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    if not parts:
        return []
    index = 0
    if Path(parts[0]).name == "env":
        index = 1
        while index < len(parts) and "=" in parts[index] and not parts[index].startswith("/"):
            index += 1
    if index >= len(parts):
        return []
    identities = [Path(parts[index]).name]
    if Path(parts[index]).name == "flatpak":
        for part in parts[index + 1 :]:
            if not part.startswith("-") and part not in {"run"}:
                identities.append(part)
                break
    return list(dict.fromkeys(item for item in identities if item))


def desktop_entries() -> dict[str, dict[str, Any]]:
    """Read launchable desktop apps with private identities used for matching."""

    global _DESKTOP_ENTRY_CACHE, _DESKTOP_ENTRY_CACHE_KEY, _DESKTOP_ENTRY_CACHE_AT
    entries: dict[str, dict[str, Any]] = {}
    shadowed: set[str] = set()
    data_roots = [Path.home() / ".local/share"]
    data_roots.extend(
        Path(item)
        for item in os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":")
        if item
    )
    roots = list(dict.fromkeys(root / "applications" for root in data_roots))
    cache_key = tuple(str(root) for root in roots)
    now = time.monotonic()
    if cache_key == _DESKTOP_ENTRY_CACHE_KEY and now - _DESKTOP_ENTRY_CACHE_AT <= 15.0:
        return deepcopy(_DESKTOP_ENTRY_CACHE)
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.desktop"):
            desktop_id = str(path.relative_to(root).with_suffix("")).replace(os.sep, "-")
            if desktop_id in shadowed:
                continue
            parser = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                parser.read(path, encoding="utf-8")
                section = parser["Desktop Entry"]
                shadowed.add(desktop_id)
                if (
                    section.get("Type") != "Application"
                    or section.getboolean("NoDisplay", fallback=False)
                    or section.getboolean("Hidden", fallback=False)
                ):
                    continue
                command = section.get("Exec", "")
                entries[desktop_id] = {
                    "desktop_id": desktop_id,
                    "name": section.get("Name", desktop_id),
                    "generic_name": section.get("GenericName", ""),
                    "icon": section.get("Icon", ""),
                    "_comment": section.get("Comment", ""),
                    "_keywords": [item for item in section.get("Keywords", "").split(";") if item],
                    "_startup_wm_class": section.get("StartupWMClass", ""),
                    "_exec_identities": _exec_identities(command),
                }
            except (OSError, configparser.Error, ValueError):
                continue
    _DESKTOP_ENTRY_CACHE = deepcopy(entries)
    _DESKTOP_ENTRY_CACHE_KEY = cache_key
    _DESKTOP_ENTRY_CACHE_AT = now
    return entries


def _application_fields(
    entry: dict[str, Any],
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    primary = [
        ("name", str(entry.get("name", ""))),
        ("generic_name", str(entry.get("generic_name", ""))),
        ("desktop_id", str(entry.get("desktop_id", ""))),
        ("startup_wm_class", str(entry.get("_startup_wm_class", ""))),
        *(("executable", str(item)) for item in entry.get("_exec_identities", [])),
    ]
    secondary = [
        ("comment", str(entry.get("_comment", ""))),
        *(("keyword", str(item)) for item in entry.get("_keywords", [])),
    ]
    return (
        [(name, value) for name, value in primary if value],
        [(name, value) for name, value in secondary if value],
    )


def _application_match(entry: dict[str, Any], query: str) -> tuple[int, list[str]]:
    query_key = _application_key(query)
    if not query_key:
        return 0, []
    query_tokens = set(query_key.split())
    primary, secondary = _application_fields(entry)
    primary_keys = [(name, _application_key(value)) for name, value in primary]
    secondary_keys = [(name, _application_key(value)) for name, value in secondary]
    exact = [name for name, key in primary_keys if key == query_key]
    if exact:
        return 100, exact
    token_matches = [name for name, key in primary_keys if query_tokens.issubset(set(key.split()))]
    if token_matches:
        return 84, token_matches
    contained = [name for name, key in primary_keys if query_key in key or key in query_key]
    if contained:
        return 74, contained
    metadata = [name for name, key in secondary_keys if query_tokens.issubset(set(key.split()))]
    if metadata:
        return 66, metadata
    ratios = [
        (SequenceMatcher(None, query_key, key).ratio(), name) for name, key in primary_keys if key
    ]
    ratio, field = max(ratios, default=(0.0, ""))
    if ratio >= 0.82:
        return min(72, int(50 + ratio * 25)), [field]
    return 0, []


def _running_process_keys() -> dict[str, set[int]]:
    global _PROCESS_KEY_CACHE, _PROCESS_KEY_CACHE_AT
    now = time.monotonic()
    if now - _PROCESS_KEY_CACHE_AT <= 1.0:
        return {key: set(pids) for key, pids in _PROCESS_KEY_CACHE.items()}
    keys: dict[str, set[int]] = {}
    attributes = ["pid", "uids", "name", "exe", "cmdline", "status"]
    for process in psutil.process_iter(attributes):
        try:
            info = process.info
            uids = info.get("uids")
            if uids is not None and int(uids.real) != os.getuid():
                continue
            if info.get("status") == psutil.STATUS_ZOMBIE:
                continue
            values = [
                str(info.get("name") or ""),
                Path(str(info.get("exe") or "")).name,
                *_script_argv_identity([str(item) for item in (info.get("cmdline") or [])]),
            ]
            for value in values:
                key = _application_key(value)
                if key:
                    keys.setdefault(key, set()).add(int(info["pid"]))
        except (psutil.AccessDenied, psutil.NoSuchProcess, AttributeError, ValueError):
            continue
    _PROCESS_KEY_CACHE = {key: set(pids) for key, pids in keys.items()}
    _PROCESS_KEY_CACHE_AT = now
    return keys


def _entry_identity_keys(entry: dict[str, Any]) -> set[str]:
    primary, _secondary = _application_fields(entry)
    keys = {
        _application_key(value)
        for name, value in primary
        if name != "generic_name" and _application_key(value)
    }
    # Shared launchers/interpreters cannot prove an application's identity.
    return {
        key
        for key in keys
        if not re.fullmatch(
            r"(?:python(?:\s*\d+)*|pythonw|sh|bash|env|flatpak|java|javaw|node|electron(?:\s*\d+)*|wine(?:\s*\d+)*|wine\s*preloader|gtk\s*launch)",
            key,
        )
    }


def _public_application(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in entry.items() if not key.startswith("_")}


async def list_applications(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    query = str(arguments.get("query", "")).strip()
    limit = int(arguments.get("limit", 50))
    entries = list((await asyncio.to_thread(desktop_entries)).values())
    if bool(arguments.get("launch_only", False)):
        # Opening a known installed app does not require waiting for KWin or
        # walking every process. Resolve the trusted catalog; ambiguous matches
        # still follow the existing confidence/identity checks.
        result = await asyncio.to_thread(_build_application_catalog, entries, {}, [], query, limit)
        result["sources"] = ["desktop_entries"]
        result["running_state_observed"] = False
        for entry in result["applications"]:
            entry.pop("running", None)
            entry.pop("window_ids", None)
            entry.pop("pids", None)
        return result

    process_keys = await asyncio.to_thread(_running_process_keys)
    windows: list[dict[str, Any]] = []
    if context is not None and context.desktop is not None:
        try:
            world = await context.desktop.snapshot(force=True)
            windows = context.desktop.visible_windows(world)
        except Exception as error:
            context.logger.debug("Application catalog could not refresh KWin: %s", error)

    return await asyncio.to_thread(
        _build_application_catalog, entries, process_keys, windows, query, limit
    )


def _build_application_catalog(
    entries: list[dict[str, Any]],
    process_keys: dict[str, set[int]],
    windows: list[dict[str, Any]],
    query: str,
    limit: int,
) -> dict[str, Any]:
    observed: list[dict[str, Any]] = []
    associated_windows: set[str] = set()
    for entry in entries:
        identity_keys = _entry_identity_keys(entry)
        pids: set[int] = set()
        for identity in identity_keys:
            pids.update(process_keys.get(identity, set()))
        entry_windows: list[dict[str, Any]] = []
        for window in windows:
            window_keys = {
                _application_key(str(window.get("app_id", ""))),
                _application_key(str(window.get("resource_class", ""))),
                _application_key(str(window.get("resource_name", ""))),
            } - {""}
            if identity_keys.intersection(window_keys):
                entry_windows.append(window)
                associated_windows.add(str(window.get("id", "")))
                if int(window.get("pid") or 0) > 1:
                    pids.add(int(window["pid"]))
        entry.update(
            {
                "running": bool(pids or entry_windows),
                "pids": sorted(pids),
                "window_count": len(entry_windows),
                "window_ids": [str(window.get("id", "")) for window in entry_windows],
            }
        )

    for window in windows:
        window_id = str(window.get("id", ""))
        if window_id in associated_windows:
            continue
        identity = str(
            window.get("app_id")
            or window.get("resource_class")
            or window.get("resource_name")
            or ""
        ).strip()
        if not identity:
            continue
        observed.append(
            {
                "name": identity,
                "running": True,
                "pid": int(window.get("pid") or 0),
                "window_id": window_id,
                "launchable": False,
            }
        )

    if query:
        matches = []
        for entry in entries:
            score, fields = _application_match(entry, query)
            if not score:
                continue
            matches.append(
                {**_public_application(entry), "match_score": score, "matched_fields": fields}
            )
        entries = matches
        entries.sort(
            key=lambda entry: (
                -int(entry["match_score"]),
                not bool(entry.get("running")),
                entry["name"].casefold(),
            )
        )
        observed = [
            item
            for item in observed
            if _application_match({"name": item["name"], "desktop_id": ""}, query)[0]
        ]
    else:
        entries.sort(key=lambda entry: entry["name"].casefold())
        entries = [_public_application(entry) for entry in entries]
    return {
        "applications": entries[:limit],
        "matches": len(entries),
        "observed_running": observed[:limit],
        "sources": ["desktop_entries", "kwin_windows", "user_processes"],
    }


def open_application(arguments: dict[str, Any], _context: ToolContext) -> dict[str, Any]:
    desktop_id = arguments["desktop_id"]
    entries = desktop_entries()
    if desktop_id not in entries:
        raise ValidationError(f"unknown desktop application: {desktop_id}")
    started = time.monotonic()
    try:
        # GTK may pass its standard descriptors to the long-lived GUI app.
        # Capturing pipes waits for the app to exit even after gtk-launch has
        # successfully exited. Wait only for the launcher, never inherited EOF.
        process = subprocess.run(
            [_platform_executable("/usr/bin/gtk-launch"), desktop_id],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
            start_new_session=True,
        )
        launched = process.returncode == 0
        activation_status = "accepted" if launched else "failed"
        detail = (
            "Desktop entry activation accepted"
            if launched
            else f"Desktop launcher exited with status {process.returncode}"
        )
    except subprocess.TimeoutExpired:
        launched = False
        activation_status = "unverified"
        detail = "Desktop launcher did not acknowledge in time; the application may still have opened. No retry was issued."
    return {
        "launched": launched,
        "desktop_id": desktop_id,
        "name": entries[desktop_id].get("name", desktop_id),
        "activation_status": activation_status,
        "detail": detail,
        "verification": "launcher_acceptance_only",
        "duration_ms": round((time.monotonic() - started) * 1000, 3),
    }


def focus_application(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    result = open_application(arguments, context)
    result["focus_requested"] = True
    result["note"] = (
        "The desktop entry was activated; single-instance applications normally focus their existing window."
    )
    return result


def _same_process_is_alive(process: psutil.Process, expected_start: float) -> bool:
    try:
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return False
        return abs(process.create_time() - expected_start) <= 0.01
    except psutil.NoSuchProcess:
        return False


def _wait_for_process_exit(process: psutil.Process, expected_start: float, timeout: float) -> bool:
    try:
        process.wait(timeout=timeout)
        return True
    except psutil.NoSuchProcess:
        return True
    except psutil.TimeoutExpired:
        return not _same_process_is_alive(process, expected_start)


def _run_close_command(arguments: list[str], timeout: float) -> bool:
    try:
        return bool(run_command(arguments, timeout=timeout).get("ok"))
    except (OSError, subprocess.SubprocessError):
        return False


def _request_spotify_mpris_quit() -> bool:
    try:
        probe = run_command(
            [
                _platform_executable("/usr/bin/qdbus6"),
                _SPOTIFY_MPRIS_SERVICE,
                "/org/mpris/MediaPlayer2",
                "org.freedesktop.DBus.Properties.Get",
                "org.mpris.MediaPlayer2",
                "CanQuit",
            ],
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if not probe.get("ok") or probe.get("stdout", "").strip().casefold() not in {"true", "1"}:
        return False
    return _run_close_command(
        [
            _platform_executable("/usr/bin/qdbus6"),
            _SPOTIFY_MPRIS_SERVICE,
            "/org/mpris/MediaPlayer2",
            "org.mpris.MediaPlayer2.Quit",
        ],
        timeout=3,
    )


def _close_result(
    pid: int,
    name: str,
    username: str,
    expected_query: str,
    closed: bool,
    method: str,
    attempts: list[str],
) -> dict[str, Any]:
    return {
        "ok": closed,
        "closed": closed,
        "pid": pid,
        "name": name,
        "username": username,
        "expected_query": expected_query,
        "status": "terminated" if closed else "still_running",
        "method": method,
        "attempts": attempts,
    }


def close_process(arguments: dict[str, Any], _context: ToolContext) -> dict[str, Any]:
    pid = int(arguments["pid"])
    expected_query = str(arguments["expected_query"]).strip()
    if pid in {1, os.getpid()}:
        raise ValidationError("protected process")
    try:
        process = psutil.Process(pid)
        with process.oneshot():
            name = process.name()
            username = process.username()
            started = process.create_time()
            private_info = {
                "name": name,
                "exe": process.exe(),
                "cmdline": process.cmdline(),
            }
        if process.uids().real != os.getuid() or name in PROTECTED_PROCESSES:
            raise ValidationError(f"refusing to close protected or foreign process: {name}")
        expected_start = float(arguments["started_at_epoch"])
        if abs(expected_start - started) > 0.01:
            raise ValidationError("process identity changed before execution")
        if not _process_matches_query(private_info, expected_query):
            raise ValidationError("process no longer matches the requested application")

        attempts: list[str] = []
        spotify_target = (
            _identity_tokens(expected_query) == {"spotify"} and name.casefold() == "spotify"
        )
        if spotify_target:
            attempts.append("mpris_quit")
            if _request_spotify_mpris_quit() and _wait_for_process_exit(
                process, started, timeout=3
            ):
                return _close_result(
                    pid, name, username, expected_query, True, "mpris_quit", attempts
                )

        attempts.append("sigterm")
        process.send_signal(signal.SIGTERM)
        if _wait_for_process_exit(process, started, timeout=5):
            return _close_result(pid, name, username, expected_query, True, "sigterm", attempts)

        if spotify_target:
            attempts.append("flatpak_kill")
            if _run_close_command(
                [_platform_executable("/usr/bin/flatpak"), "kill", _SPOTIFY_FLATPAK_ID], timeout=5
            ):
                if _wait_for_process_exit(process, started, timeout=3):
                    return _close_result(
                        pid, name, username, expected_query, True, "flatpak_kill", attempts
                    )
        return _close_result(pid, name, username, expected_query, False, "none", attempts)
    except psutil.NoSuchProcess as error:
        raise ValidationError("process no longer exists") from error
    except psutil.AccessDenied as error:
        raise ValidationError("process identity could not be verified") from error


def get_volume(_arguments: dict[str, Any], _context: ToolContext) -> dict[str, Any]:
    sink = run_command([_platform_executable("/usr/bin/pactl"), "get-default-sink"], timeout=2)
    if not sink["ok"] or not sink["stdout"].strip():
        raise RuntimeError("No current audio output was found")
    return _output_state(sink["stdout"].strip())


def _output_state(sink: str) -> dict[str, Any]:
    matches = [row for row in _pactl_json(["list", "sinks"]) if row.get("name") == sink]
    if len(matches) != 1:
        raise RuntimeError(
            "The selected audio output disconnected; no replacement output was changed"
        )
    endpoint = _audio_endpoint(matches[0])
    if endpoint["percent"] is None:
        raise RuntimeError("Audio output volume could not be read")
    return {
        "sink": sink,
        "percent": endpoint["percent"],
        "muted": endpoint["muted"],
        "message": f"Volume is {endpoint['percent']}%"
        + (" and muted." if endpoint["muted"] else "."),
    }


def _set_output_percent(before: dict[str, Any], percent: int) -> dict[str, Any]:
    result = run_command(
        [_platform_executable("/usr/bin/pactl"), "set-sink-volume", before["sink"], f"{percent}%"],
        timeout=2,
    )
    after = _output_state(before["sink"])
    verified = bool(result["ok"] and abs(after["percent"] - percent) <= 1)
    return {
        **after,
        "set": verified,
        "verified": verified,
        "requested_percent": percent,
        "message": (
            after["message"] if verified else "The output did not reach the requested volume."
        ),
    }


def set_volume(arguments: dict[str, Any], _context: ToolContext) -> dict[str, Any]:
    percent = int(arguments["percent"])
    if not 0 <= percent <= 100:
        raise ValueError("Normal output volume must be between 0 and 100 percent")
    with _OUTPUT_CONTROL_LOCK:
        return _set_output_percent(get_volume({}, _context), percent)


def adjust_volume(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    delta = int(arguments["delta"])
    if not -100 <= delta <= 100 or not delta:
        raise ValueError("Specify a nonzero volume adjustment up to 100 percentage points")
    with _OUTPUT_CONTROL_LOCK:
        before = get_volume({}, context)
        return _set_output_percent(before, max(0, min(100, before["percent"] + delta)))


def set_mute(arguments: dict[str, Any], _context: ToolContext) -> dict[str, Any]:
    muted = bool(arguments["muted"])
    with _OUTPUT_CONTROL_LOCK:
        before = get_volume({}, _context)
        result = run_command(
            [
                _platform_executable("/usr/bin/pactl"),
                "set-sink-mute",
                before["sink"],
                "1" if muted else "0",
            ],
            timeout=2,
        )
        after = _output_state(before["sink"])
        verified = bool(result["ok"] and after["muted"] == muted)
        return {
            **after,
            "set": verified,
            "verified": verified,
            "message": (
                ("Output muted." if muted else "Output unmuted.")
                if verified
                else "The requested mute state did not verify."
            ),
        }


def _pactl_json(arguments: list[str]) -> Any:
    result = run_command(
        [_platform_executable("/usr/bin/pactl"), "-f", "json", *arguments],
        timeout=8,
        max_output=262_144,
    )
    if not result["ok"]:
        raise RuntimeError(result["stderr"].strip() or "PipeWire/PulseAudio query failed")
    try:
        return json.loads(result["stdout"])
    except json.JSONDecodeError as error:
        raise RuntimeError("PipeWire/PulseAudio returned invalid device data") from error


def _audio_endpoint(item: dict[str, Any]) -> dict[str, Any]:
    volume = item.get("volume", {})
    percentages: list[int] = []
    for channel in volume.values() if isinstance(volume, dict) else []:
        match = re.search(r"(\d+)%", str((channel or {}).get("value_percent", "")))
        if match:
            percentages.append(int(match.group(1)))
    properties = item.get("properties", {}) if isinstance(item.get("properties"), dict) else {}
    return {
        "index": int(item.get("index", -1)),
        "name": str(item.get("name", "")),
        "description": str(item.get("description", "")),
        "state": str(item.get("state", "UNKNOWN")),
        "muted": bool(item.get("mute", False)),
        "percent": round(sum(percentages) / len(percentages)) if percentages else None,
        "device_bus": str(properties.get("device.bus", "")),
        "device_description": str(properties.get("device.description", "")),
    }


def get_audio_devices(_arguments: dict[str, Any], _context: ToolContext) -> dict[str, Any]:
    info = _pactl_json(["info"])
    sources = [_audio_endpoint(item) for item in _pactl_json(["list", "sources"])]
    sinks = [_audio_endpoint(item) for item in _pactl_json(["list", "sinks"])]
    microphones = [item for item in sources if not item["name"].endswith(".monitor")]
    return {
        "backend": str(info.get("server_name", "")),
        "default_input": str(info.get("default_source_name", "")),
        "default_output": str(info.get("default_sink_name", "")),
        "inputs": microphones,
        "outputs": sinks,
    }


def set_default_audio_input(arguments: dict[str, Any], _context: ToolContext) -> dict[str, Any]:
    source = str(arguments["source"])
    devices = get_audio_devices({}, _context)
    if source not in {item["name"] for item in devices["inputs"]}:
        raise ValidationError("input source is unavailable or is an output monitor")
    result = run_command(
        [_platform_executable("/usr/bin/pactl"), "set-default-source", source], timeout=5
    )
    actual = run_command([_platform_executable("/usr/bin/pactl"), "get-default-source"], timeout=5)[
        "stdout"
    ].strip()
    return {
        "verified": result["ok"] and actual == source,
        "source": actual,
        "requested": source,
        "detail": result["stderr"].strip(),
    }


def set_default_audio_output(arguments: dict[str, Any], _context: ToolContext) -> dict[str, Any]:
    sink = str(arguments["sink"])
    devices = get_audio_devices({}, _context)
    if sink not in {item["name"] for item in devices["outputs"]}:
        raise ValidationError("output sink is unavailable")
    result = run_command(
        [_platform_executable("/usr/bin/pactl"), "set-default-sink", sink], timeout=5
    )
    actual = run_command([_platform_executable("/usr/bin/pactl"), "get-default-sink"], timeout=5)[
        "stdout"
    ].strip()
    return {
        "verified": result["ok"] and actual == sink,
        "sink": actual,
        "requested": sink,
        "detail": result["stderr"].strip(),
    }


def set_microphone_mute(arguments: dict[str, Any], _context: ToolContext) -> dict[str, Any]:
    source = str(arguments.get("source", "")).strip() or "@DEFAULT_SOURCE@"
    if source != "@DEFAULT_SOURCE@":
        devices = get_audio_devices({}, _context)
        if source not in {item["name"] for item in devices["inputs"]}:
            raise ValidationError("input source is unavailable or is an output monitor")
    muted = bool(arguments["muted"])
    result = run_command(
        [_platform_executable("/usr/bin/pactl"), "set-source-mute", source, "1" if muted else "0"],
        timeout=5,
    )
    observed = run_command(
        [_platform_executable("/usr/bin/pactl"), "get-source-mute", source], timeout=5
    )
    actual = observed["stdout"].strip().endswith("yes")
    return {
        "verified": result["ok"] and observed["ok"] and actual == muted,
        "source": source,
        "muted": actual,
        "requested": muted,
        "detail": result["stderr"].strip(),
    }


def get_system_identity(_arguments: dict[str, Any], _context: ToolContext) -> dict[str, Any]:
    release: dict[str, str] = {}
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                release[key] = value.strip().strip("\"'")
    except OSError:
        pass
    uname = platform.uname()
    return {
        "operating_system": release.get("PRETTY_NAME", platform.system()),
        "kernel": uname.release,
        "architecture": uname.machine,
        "hostname": uname.node,
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP", ""),
        "session_type": os.environ.get("XDG_SESSION_TYPE", ""),
    }


def get_mounts(_arguments: dict[str, Any], _context: ToolContext) -> dict[str, Any]:
    mounts = []
    for item in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(item.mountpoint)
        except (OSError, PermissionError):
            continue
        mounts.append(
            {
                "device": item.device,
                "mountpoint": item.mountpoint,
                "filesystem": item.fstype,
                "options": item.opts,
                "total_bytes": usage.total,
                "free_bytes": usage.free,
                "percent": usage.percent,
            }
        )
    return {"mounts": mounts, "count": len(mounts)}


def get_connected_devices(_arguments: dict[str, Any], _context: ToolContext) -> dict[str, Any]:
    usb = run_command([_platform_executable("/usr/bin/lsusb")], timeout=5)
    bluetooth = run_command(
        [_platform_executable("/usr/bin/bluetoothctl"), "devices", "Connected"], timeout=5
    )
    return {
        "usb": [line.strip() for line in usb["stdout"].splitlines() if line.strip()],
        "bluetooth": [
            line.removeprefix("Device ").strip()
            for line in bluetooth["stdout"].splitlines()
            if line.startswith("Device ")
        ],
        "audio": get_audio_devices({}, _context),
        "limitations": [
            "USB entries identify buses and products, not trust",
            "Bluetooth reports current BlueZ connections",
        ],
    }


def get_openrc_services(_arguments: dict[str, Any], _context: ToolContext) -> dict[str, Any]:
    result = run_command(
        [_platform_executable("/usr/bin/rc-status"), "-a"], timeout=8, max_output=65536
    )
    services: list[dict[str, str]] = []
    runlevel = ""
    for line in result["stdout"].splitlines():
        if line.startswith("Runlevel:"):
            runlevel = line.partition(":")[2].strip()
            continue
        match = re.match(r"\s*(\S.*?)\s+\[\s*(\w+)\s*\]\s*$", line)
        if match:
            services.append(
                {"name": match.group(1).strip(), "status": match.group(2), "runlevel": runlevel}
            )
    return {
        "ok": result["ok"],
        "services": services,
        "count": len(services),
        "detail": result["stderr"].strip(),
    }


def mpris(arguments: dict[str, Any], _context: ToolContext) -> dict[str, Any]:
    action = arguments["action"]
    requested = str(arguments.get("player", "")).casefold().strip()

    def matching_services() -> tuple[list[str], str]:
        listing = run_command([_platform_executable("/usr/bin/qdbus6")], timeout=2)
        if not listing.get("ok"):
            return [], "The desktop media service is unavailable"
        services = sorted(
            line.strip()
            for line in listing["stdout"].splitlines()
            if line.strip().startswith("org.mpris.MediaPlayer2.")
        )
        if requested:
            exact = f"org.mpris.MediaPlayer2.{requested}".casefold()
            services = [
                service
                for service in services
                if service.casefold() == exact or service.casefold().startswith(exact + ".")
            ]
        return services, ""

    services, service_error = matching_services()
    launched = False
    if not services and requested == "spotify" and action == "play":
        try:
            launch = open_application({"desktop_id": _SPOTIFY_FLATPAK_ID}, _context)
        except (ValidationError, OSError) as error:
            launch = {"launched": False}
            service_error = str(error)
        if launch.get("launched"):
            launched = True
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                time.sleep(0.25)
                services, service_error = matching_services()
                if services:
                    break
    if not services:
        target = requested.title() if requested else "A compatible media player"
        return {
            "ok": False,
            "action": action,
            "launched": launched,
            "reason": service_error or f"{target} is not open or available for media control",
        }
    method = {
        "play": "Play",
        "pause": "Pause",
        "toggle": "PlayPause",
        "next": "Next",
        "previous": "Previous",
    }[action]
    service = services[0]
    result = run_command(
        [
            _platform_executable("/usr/bin/qdbus6"),
            service,
            "/org/mpris/MediaPlayer2",
            f"org.mpris.MediaPlayer2.Player.{method}",
        ]
    )
    if not result["ok"]:
        return {
            "ok": False,
            "player": service,
            "action": action,
            "reason": result["stderr"].strip() or f"{method} was rejected by the media player",
        }

    status = ""
    if action in {"play", "pause"}:
        expected = "playing" if action == "play" else "paused"
        for _attempt in range(5):
            state = run_command(
                [
                    _platform_executable("/usr/bin/qdbus6"),
                    service,
                    "/org/mpris/MediaPlayer2",
                    "org.freedesktop.DBus.Properties.Get",
                    "org.mpris.MediaPlayer2.Player",
                    "PlaybackStatus",
                ],
                timeout=2,
            )
            status = state.get("stdout", "").strip()
            if state.get("ok") and status.casefold() == expected:
                break
            time.sleep(0.1)
        if status.casefold() != expected:
            return {
                "ok": False,
                "player": service,
                "action": action,
                "playback_status": status,
                "reason": f"Spotify did not report that playback was {expected}",
            }
    return {
        "ok": True,
        "player": service,
        "action": action,
        "playback_status": status,
        "launched": launched,
    }


def find_file(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    query = arguments["query"].casefold()
    root = resolve_allowed(arguments.get("root", str(Path.home() / "Downloads")), context)
    limit = int(arguments.get("limit", 50))
    include_hidden = bool(arguments.get("include_hidden", False))
    results: list[dict[str, Any]] = []
    visited = 0
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = [
            name
            for name in directories
            if name not in {".git", "node_modules", "build", "target"}
            and (include_hidden or not name.startswith("."))
        ]
        for name in files:
            visited += 1
            if not include_hidden and name.startswith("."):
                continue
            if query in name.casefold():
                path = Path(current) / name
                try:
                    stat = path.stat()
                except OSError:
                    continue
                results.append(
                    {
                        "name": name,
                        "path": str(path),
                        "size_bytes": stat.st_size,
                        "modified_epoch": stat.st_mtime,
                    }
                )
                if len(results) >= limit:
                    return {"results": results, "visited_files": visited, "truncated": True}
    return {"results": results, "visited_files": visited, "truncated": False}


def get_file_info(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    path = resolve_allowed(arguments["path"], context)
    stat = path.stat()
    return {
        "path": str(path),
        "name": path.name,
        "is_file": path.is_file(),
        "is_directory": path.is_dir(),
        "size_bytes": stat.st_size,
        "modified_epoch": stat.st_mtime,
        "mode": oct(stat.st_mode & 0o777),
    }


def read_file(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    path = resolve_allowed(arguments["path"], context)
    if not path.is_file():
        raise ValidationError("path is not a regular file")
    maximum = min(
        int(arguments.get("max_bytes", context.config["security"]["max_file_read_bytes"])),
        int(context.config["security"]["max_file_read_bytes"]),
    )
    with path.open("rb") as handle:
        data = handle.read(maximum + 1)
    truncated = len(data) > maximum
    data = data[:maximum]
    if b"\0" in data:
        raise ValidationError("binary file reading is not supported")
    return {
        "path": str(path),
        "content": data.decode("utf-8", errors="replace"),
        "bytes": len(data),
        "truncated": truncated,
    }


def open_file(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    path = resolve_allowed(arguments["path"], context)
    process = subprocess.Popen(
        [_platform_executable("/usr/bin/xdg-open"), str(path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return {"opened": True, "path": str(path), "launcher_pid": process.pid}


def list_directory(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    path = resolve_allowed(arguments["path"], context)
    if not path.is_dir():
        raise ValidationError("path is not a directory")
    include_hidden = bool(arguments.get("include_hidden", False))
    limit = int(arguments.get("limit", 100))
    entries: list[dict[str, Any]] = []
    for child in sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold())):
        if not include_hidden and child.name.startswith("."):
            continue
        try:
            stat = child.lstat()
        except OSError:
            continue
        entries.append(
            {
                "name": child.name,
                "path": str(child),
                "is_file": child.is_file(),
                "is_directory": child.is_dir(),
                "is_symlink": child.is_symlink(),
                "size_bytes": stat.st_size,
                "modified_epoch": stat.st_mtime,
            }
        )
        if len(entries) >= limit:
            break
    return {
        "path": str(path),
        "entries": entries,
        "count": len(entries),
        "truncated": len(entries) >= limit,
    }


def create_directory(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    path = resolve_destination(arguments["path"], context)
    if path.exists():
        return {
            "verified": path.is_dir(),
            "created": False,
            "path": str(path),
            "reason": "already_exists",
        }
    path.mkdir(parents=bool(arguments.get("parents", False)), mode=0o755)
    return {"verified": path.is_dir(), "created": True, "path": str(path)}


def create_text_file(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    path = resolve_destination(arguments["path"], context)
    if path.exists() or path.is_symlink():
        raise ValidationError("destination already exists; E.V. will not overwrite it")
    content = str(arguments["content"])
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
        handle.write(content)
    actual = path.read_text(encoding="utf-8")
    return {
        "verified": actual == content,
        "created": True,
        "path": str(path),
        "bytes": path.stat().st_size,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_file(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    path = resolve_allowed(arguments["path"], context)
    if not path.is_file():
        raise ValidationError("path is not a regular file")
    return {
        "path": str(path),
        "algorithm": "sha256",
        "sha256": _sha256(path),
        "size_bytes": path.stat().st_size,
    }


def copy_path(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    original = Path(arguments["source"]).expanduser()
    if original.is_symlink():
        raise ValidationError("copying symbolic links is not supported")
    source = resolve_allowed(arguments["source"], context)
    destination = resolve_destination(arguments["destination"], context)
    if destination.exists() or destination.is_symlink():
        raise ValidationError("destination already exists; E.V. will not overwrite it")
    if source.is_symlink():
        raise ValidationError("copying symbolic links is not supported")
    if source.is_dir():
        if destination.is_relative_to(source):
            raise ValidationError("destination cannot be inside the source directory")
        # Preflight links and special files before creating any destination.
        # Preserve links if a source races after preflight; never dereference
        # a newly introduced link into an arbitrary external directory.
        manifest = {}
        for current, directories, files in os.walk(source, followlinks=False):
            for name in [*directories, *files]:
                entry = Path(current) / name
                if entry.is_symlink() or not (entry.is_dir() or entry.is_file()):
                    raise ValidationError("directory copy contains a symbolic link or special file")
                relative = str(entry.relative_to(source))
                manifest[relative] = None if entry.is_dir() else _sha256(entry)
                if len(manifest) > 10000:
                    raise ValidationError(
                        "directory copy exceeds the 10000-entry verification limit"
                    )
        shutil.copytree(source, destination, symlinks=True)
        actual = {}
        for current, directories, files in os.walk(destination, followlinks=False):
            for name in [*directories, *files]:
                entry = Path(current) / name
                relative = str(entry.relative_to(destination))
                actual[relative] = (
                    "SYMLINK" if entry.is_symlink() else None if entry.is_dir() else _sha256(entry)
                )
        verified = destination.is_dir() and actual == manifest
        kind = "directory"
        checksum = ""
    elif source.is_file():
        source_hash = _sha256(source)
        shutil.copy2(source, destination)
        checksum = _sha256(destination)
        verified = destination.is_file() and checksum == source_hash
        kind = "file"
    else:
        raise ValidationError("source is not a regular file or directory")
    return {
        "verified": verified,
        "source": str(source),
        "destination": str(destination),
        "kind": kind,
        "sha256": checksum,
    }


def move_path(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    source = resolve_allowed(arguments["source"], context)
    destination = resolve_destination(arguments["destination"], context)
    if destination.exists() or destination.is_symlink():
        raise ValidationError("destination already exists; E.V. will not overwrite it")
    if source == destination:
        raise ValidationError("source and destination are the same")
    shutil.move(str(source), str(destination))
    return {
        "verified": destination.exists() and not source.exists(),
        "source": str(source),
        "destination": str(destination),
        "recoverable": False,
    }


def trash_path(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    path = resolve_allowed(arguments["path"], context)
    if path in allowed_roots(context):
        raise ValidationError("refusing to trash an allowed-root directory")
    result = run_command([_platform_executable("/usr/bin/gio"), "trash", str(path)], timeout=30)
    return {
        "verified": result["ok"] and not path.exists(),
        "trashed": result["ok"] and not path.exists(),
        "original_path": str(path),
        "recoverable": True,
        "detail": result["stderr"].strip(),
    }


PROJECT_MARKERS = {
    ".git",
    "CMakeLists.txt",
    "meson.build",
    "Cargo.toml",
    "build.gradle",
    "build.gradle.kts",
    "gradlew",
    "pyproject.toml",
}


def find_project(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    query = arguments.get("query", "").casefold()
    root = resolve_allowed(arguments.get("root", str(Path.home() / "Downloads")), context)
    limit = int(arguments.get("limit", 30))
    results = []
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = [
            name
            for name in directories
            if name not in {"node_modules", "build", "target"} and not name.startswith(".")
        ]
        names = set(directories) | set(files)
        markers = sorted(PROJECT_MARKERS & names)
        path = Path(current)
        if markers and (
            not query or query in path.name.casefold() or query in str(path).casefold()
        ):
            results.append({"name": path.name, "path": str(path), "markers": markers})
            directories[:] = []
            if len(results) >= limit:
                break
    return {"projects": results, "truncated": len(results) >= limit}


def get_git_status(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    root = resolve_allowed(arguments["project"], context)
    result = run_command(
        [_platform_executable("/usr/bin/git"), "-C", str(root), "status", "--short", "--branch"],
        timeout=15,
    )
    return {"project": str(root), **result}


def build_project(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    # Old clients still use this name. It must go through the same
    # approved sandbox runner.
    return {
        "ok": False,
        "executed": False,
        "error": "The legacy unsandboxed build endpoint is retired. Inspect the project with development.project.inspect, then request development.project.run with its manifest hash. That runner requires approval; unsupported toolchains are not executed.",
        "replacement_tools": ["development.project.inspect", "development.project.run"],
        "error_code": "retired_build_endpoint",
    }


def inspect_build_error(arguments: dict[str, Any], _context: ToolContext) -> dict[str, Any]:
    text = arguments["text"]
    patterns = re.compile(
        r"(^|\s)(error:|fatal:|undefined reference|FAILED:|exception|traceback)", re.IGNORECASE
    )
    lines = text.splitlines()
    matches = []
    for index, line in enumerate(lines):
        if patterns.search(line):
            matches.append(
                {
                    "line": index + 1,
                    "text": line[:1000],
                    "context": lines[max(0, index - 1) : min(len(lines), index + 2)],
                }
            )
            if len(matches) >= 30:
                break
    return {
        "matches": matches,
        "line_count": len(lines),
        "summary": (
            "No common build-error marker found"
            if not matches
            else f"Found {len(matches)} likely error locations"
        ),
    }


def send_notification(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    if not bool(context.config.get("notifications", {}).get("enabled", False)):
        context.bus.publish(
            "desktop.notification_internal",
            "desktop",
            {
                "title": arguments["title"],
                "message": arguments["message"],
                "delivery": "application",
            },
        )
        return {
            "sent": False,
            "internal": True,
            "detail": "Desktop notifications are disabled; kept in E.V.",
        }
    command = [
        _platform_executable("/usr/bin/notify-send"),
        "-a",
        "E.V.",
        arguments["title"],
        arguments["message"],
    ]
    result = run_command(command, timeout=5)
    return {"sent": result["ok"], "detail": result["stderr"].strip()}


def clipboard_read(_arguments: dict[str, Any], _context: ToolContext) -> dict[str, Any]:
    result = run_command(
        [
            _platform_executable("/usr/bin/qdbus6"),
            "org.kde.klipper",
            "/klipper",
            "org.kde.klipper.klipper.getClipboardContents",
        ],
        timeout=5,
        max_output=65_536,
    )
    return {
        "ok": result["ok"],
        "content": result["stdout"].rstrip("\n"),
        "truncated": result["truncated"],
    }


def clipboard_write(arguments: dict[str, Any], _context: ToolContext) -> dict[str, Any]:
    result = run_command(
        [
            _platform_executable("/usr/bin/qdbus6"),
            "org.kde.klipper",
            "/klipper",
            "org.kde.klipper.klipper.setClipboardContents",
            arguments["content"],
        ],
        timeout=5,
    )
    return {"ok": result["ok"], "detail": result["stderr"].strip()}


def window_information(_arguments: dict[str, Any], _context: ToolContext) -> dict[str, Any]:
    output = run_command(
        [
            _platform_executable("/usr/bin/qdbus6"),
            "org.kde.KWin",
            "/KWin",
            "org.kde.KWin.activeOutputName",
        ],
        timeout=5,
    )
    desktop = run_command(
        [
            _platform_executable("/usr/bin/qdbus6"),
            "org.kde.KWin",
            "/KWin",
            "org.kde.KWin.currentDesktop",
        ],
        timeout=5,
    )
    return {
        "active_output": output["stdout"].strip(),
        "current_desktop": int(desktop["stdout"].strip() or 0),
        "wayland": bool(os.environ.get("WAYLAND_DISPLAY")),
    }


def _desktop(context: ToolContext) -> Any:
    if context.desktop is None:
        raise RuntimeError("The KDE desktop world model is unavailable")
    return context.desktop


async def desktop_world(_arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return await _desktop(context).snapshot(force=True)


async def desktop_list_windows(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    model = _desktop(context)
    world = await model.snapshot(force=True)
    windows = model.visible_windows(world)
    query = str(arguments.get("query", "")).strip()
    if query:
        try:
            windows = [model.resolve_window(query, world)]
        except ValueError as error:
            return {
                "windows": [],
                "query": query,
                "error": str(error),
                "candidates": getattr(error, "candidates", []),
            }
    return {
        "windows": windows[: int(arguments.get("limit", 100))],
        "active_window_id": world.get("active_window_id", ""),
        "captured_at_monotonic": world.get("captured_at_monotonic"),
    }


async def desktop_resolve_window(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    model = _desktop(context)
    world = await model.snapshot(force=True)
    try:
        window = model.resolve_window(
            arguments["description"],
            world,
            exclude_ids=(
                {arguments["exclude_window_id"]} if arguments.get("exclude_window_id") else None
            ),
        )
    except ValueError as error:
        return {
            "resolved": False,
            "error": str(error),
            "candidates": getattr(error, "candidates", []),
        }
    return {"resolved": True, "window": window, "confidence": "HIGH"}


async def desktop_resolve_output(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    model = _desktop(context)
    world = await model.snapshot(force=True)
    try:
        output = model.resolve_output(arguments["description"], world)
    except ValueError as error:
        return {
            "resolved": False,
            "error": str(error),
            "candidates": getattr(error, "candidates", []),
        }
    return {"resolved": True, "output": output, "confidence": "HIGH"}


async def desktop_resolve_workspace(
    arguments: dict[str, Any], context: ToolContext
) -> dict[str, Any]:
    model = _desktop(context)
    world = await model.snapshot(force=True)
    try:
        desktop = model.resolve_desktop(arguments["description"], world)
    except ValueError as error:
        return {
            "resolved": False,
            "error": str(error),
            "candidates": getattr(error, "candidates", []),
        }
    return {"resolved": True, "desktop": desktop, "confidence": "HIGH"}


async def desktop_wait_for_window(
    arguments: dict[str, Any], context: ToolContext
) -> dict[str, Any]:
    model = _desktop(context)
    deadline = time.monotonic() + float(arguments.get("timeout_seconds", 8))
    last_error = "Application has not exposed a window yet"
    while time.monotonic() < deadline:
        try:
            world = await model.snapshot(force=True)
            window = model.resolve_window(arguments["description"], world)
            return {"resolved": True, "window": window}
        except ValueError as error:
            last_error = str(error)
        await asyncio.sleep(0.15)
    return {"resolved": False, "error": last_error, "candidates": []}


async def _window_after(
    context: ToolContext, window_id: str, delay: float = 0.12
) -> dict[str, Any] | None:
    await asyncio.sleep(delay)
    model = _desktop(context)
    model.invalidate()
    world = await model.snapshot(force=True)
    return next(
        (item for item in model.visible_windows(world) if str(item.get("id")) == window_id), None
    )


async def desktop_activate_window(
    arguments: dict[str, Any], context: ToolContext
) -> dict[str, Any]:
    window_id = str(arguments["window_id"])
    await _desktop(context).bridge.request("activate", {"window_id": window_id})
    actual = await _window_after(context, window_id)
    backend = "kwin-script"
    runner_error = ""
    if not actual or not actual.get("active"):
        # KWin can reject a script-originated focus change under Wayland's
        # focus-stealing prevention even though this same exact internal ID is
        # exposed by KWin's own window runner.  Ask that trusted in-process
        # runner to activate only the already-resolved ID, then re-read KWin.
        # No title guessing, shell parsing, or synthetic key is involved.
        try:
            result = await asyncio.to_thread(
                run_command,
                [
                    _platform_executable("/usr/bin/qdbus6"),
                    "org.kde.KWin",
                    "/WindowsRunner",
                    "org.kde.krunner1.Run",
                    f"0_{window_id}",
                    "",
                ],
                2,
            )
            if result.get("ok"):
                backend = "kwin-window-runner"
                actual = await _window_after(context, window_id, 0.18)
            else:
                runner_error = str(result.get("stderr", ""))[:500]
        except (OSError, subprocess.SubprocessError) as error:
            runner_error = str(error)[:500]
    response = {
        "verified": bool(actual and actual.get("active")),
        "window": actual,
        "activation_backend": backend,
    }
    if runner_error:
        response["activation_error"] = runner_error
    return response


async def desktop_move_resize_window(
    arguments: dict[str, Any], context: ToolContext
) -> dict[str, Any]:
    window_id = str(arguments["window_id"])
    expected = {key: int(arguments[key]) for key in ("x", "y", "width", "height")}
    model = _desktop(context)
    initial_world = await model.snapshot(force=True)
    initial = next(
        (item for item in model.visible_windows(initial_world) if str(item.get("id")) == window_id),
        None,
    )
    if initial is None:
        raise ValidationError("window no longer exists")
    model.remember_window(initial, "move_resize")
    center_x = expected["x"] + expected["width"] / 2
    center_y = expected["y"] + expected["height"] / 2
    destination = next(
        (
            output
            for output in initial_world.get("outputs", [])
            if float(output.get("geometry", {}).get("x", 0))
            <= center_x
            < float(output.get("geometry", {}).get("x", 0))
            + float(output.get("geometry", {}).get("width", 0))
            and float(output.get("geometry", {}).get("y", 0))
            <= center_y
            < float(output.get("geometry", {}).get("y", 0))
            + float(output.get("geometry", {}).get("height", 0))
        ),
        None,
    )
    # KWin's native cross-output transfer safely resolves maximize/tile state
    # before exact geometry is applied on the destination output.
    if initial and destination and initial.get("output") != destination.get("name"):
        await model.bridge.request(
            "move_to_output", {"window_id": window_id, "output": destination["name"]}
        )
        await asyncio.sleep(0.22)
    # KWin applies unmaximize asynchronously.  Give it a compositor turn before
    # setting frameGeometry or a maximized window can silently discard the move.
    await model.bridge.request("activate", {"window_id": window_id})
    await asyncio.sleep(0.08)
    await model.bridge.request("restore", {"window_id": window_id})
    await asyncio.sleep(0.16)
    await model.bridge.request("move_resize", {"window_id": window_id, **expected})
    actual = await _window_after(context, window_id, 0.18)
    actual_geometry = (actual or {}).get("geometry", {})
    differences = {
        key: int(actual_geometry.get(key, -999999)) - value for key, value in expected.items()
    }
    verified = actual is not None and all(abs(delta) <= 2 for delta in differences.values())
    return {
        "verified": verified,
        "expected": expected,
        "actual": actual_geometry,
        "differences": differences,
        "window": actual,
    }


async def desktop_set_window_state(
    arguments: dict[str, Any], context: ToolContext
) -> dict[str, Any]:
    window_id = str(arguments["window_id"])
    state = str(arguments["state"])
    model = _desktop(context)
    world = await model.snapshot(force=True)
    initial = next(
        (item for item in model.visible_windows(world) if str(item.get("id")) == window_id), None
    )
    if initial is None:
        raise ValidationError("window no longer exists")
    model.remember_window(initial, f"state:{state}")
    if state == "fullscreen":
        await model.bridge.request("fullscreen", {"window_id": window_id, "enabled": True})
    else:
        await model.bridge.request(state, {"window_id": window_id})
    actual = await _window_after(context, window_id, 0.18)
    expected_key = {
        "minimize": "minimized",
        "fullscreen": "fullscreen",
        "maximize": "maximized",
    }.get(state)
    verified = bool(actual)
    if expected_key:
        verified = bool(actual and actual.get(expected_key))
    elif state == "restore":
        verified = bool(
            actual
            and not actual.get("minimized")
            and not actual.get("fullscreen")
            and not actual.get("maximized")
        )
    return {"verified": verified, "requested_state": state, "window": actual}


async def desktop_move_window_to_output(
    arguments: dict[str, Any], context: ToolContext
) -> dict[str, Any]:
    window_id = str(arguments["window_id"])
    output = str(arguments["output"])
    model = _desktop(context)
    world = await model.snapshot(force=True)
    initial = next(
        (item for item in model.visible_windows(world) if str(item.get("id")) == window_id), None
    )
    if initial is None:
        raise ValidationError("window no longer exists")
    if not any(
        str(item.get("name")) == output and item.get("enabled", True)
        for item in world.get("outputs", [])
    ):
        raise ValidationError("output is unavailable")
    model.remember_window(initial, "move_to_output")
    await model.bridge.request("move_to_output", {"window_id": window_id, "output": output})
    actual = await _window_after(context, window_id, 0.2)
    return {
        "verified": bool(actual and actual.get("output") == output),
        "expected_output": output,
        "window": actual,
    }


async def desktop_move_window_to_workspace(
    arguments: dict[str, Any], context: ToolContext
) -> dict[str, Any]:
    window_id = str(arguments["window_id"])
    desktop_id = str(arguments["desktop_id"])
    model = _desktop(context)
    world = await model.snapshot(force=True)
    initial = next(
        (item for item in model.visible_windows(world) if str(item.get("id")) == window_id), None
    )
    if initial is None:
        raise ValidationError("window no longer exists")
    if not any(str(item.get("id")) == desktop_id for item in world.get("desktops", [])):
        raise ValidationError("virtual desktop is unavailable")
    model.remember_window(initial, "move_to_workspace")
    await model.bridge.request(
        "move_to_desktop", {"window_id": window_id, "desktop_id": desktop_id}
    )
    actual = await _window_after(context, window_id, 0.2)
    return {
        "verified": bool(actual and desktop_id in actual.get("desktops", [])),
        "expected_desktop_id": desktop_id,
        "window": actual,
    }


async def desktop_layout_window(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    window_id = str(arguments["window_id"])
    layout = str(arguments["layout"])
    model = _desktop(context)
    world = await model.snapshot(force=True)
    initial = next(
        (item for item in model.visible_windows(world) if str(item.get("id")) == window_id), None
    )
    if initial is None:
        raise ValidationError("window no longer exists")
    model.remember_window(initial, f"layout:{layout}")
    requested = await model.bridge.request("layout", {"window_id": window_id, "layout": layout})
    expected = requested.get("target_geometry", {})
    actual = await _window_after(context, window_id, 0.2)
    actual_geometry = (actual or {}).get("geometry", {})
    verified = bool(expected and actual) and all(
        abs(int(actual_geometry.get(key, -999999)) - int(expected.get(key, 999999))) <= 2
        for key in ("x", "y", "width", "height")
    )
    return {
        "verified": verified,
        "layout": layout,
        "expected": expected,
        "actual": actual_geometry,
        "window": actual,
    }


async def desktop_undo_window_change(
    _arguments: dict[str, Any], context: ToolContext
) -> dict[str, Any]:
    model = _desktop(context)
    restore = model.peek_window_restore()
    if restore is None:
        return {"verified": False, "reason": "No reversible window change is available"}
    previous = restore["window"]
    window_id = str(previous["id"])
    world = await model.snapshot(force=True)
    if not any(str(item.get("id")) == window_id for item in model.visible_windows(world)):
        return {
            "verified": False,
            "reason": "The changed window no longer exists",
            "window_id": window_id,
        }
    output = str(previous.get("output", ""))
    if output:
        await model.bridge.request("move_to_output", {"window_id": window_id, "output": output})
        await asyncio.sleep(0.12)
    desktops = [str(item) for item in previous.get("desktops", [])]
    if desktops:
        await model.bridge.request(
            "move_to_desktop", {"window_id": window_id, "desktop_id": desktops[0]}
        )
    await model.bridge.request("activate", {"window_id": window_id})
    await model.bridge.request("restore", {"window_id": window_id})
    await asyncio.sleep(0.12)
    geometry = {
        key: int(previous.get("geometry", {}).get(key, 0)) for key in ("x", "y", "width", "height")
    }
    await model.bridge.request("move_resize", {"window_id": window_id, **geometry})
    if previous.get("fullscreen"):
        await model.bridge.request("fullscreen", {"window_id": window_id, "enabled": True})
    elif previous.get("maximized"):
        await model.bridge.request("maximize", {"window_id": window_id})
    elif previous.get("minimized"):
        await model.bridge.request("minimize", {"window_id": window_id})
    actual = await _window_after(context, window_id, 0.25)
    actual_geometry = (actual or {}).get("geometry", {})
    state_ok = bool(actual) and all(
        bool(actual.get(key)) == bool(previous.get(key))
        for key in ("minimized", "fullscreen", "maximized")
    )
    placement_ok = (
        bool(actual)
        and (not output or actual.get("output") == output)
        and all(
            abs(int(actual_geometry.get(key, -999999)) - value) <= 2
            for key, value in geometry.items()
        )
    )
    desktop_ok = bool(actual) and (not desktops or desktops[0] in actual.get("desktops", []))
    verified = state_ok and placement_ok and desktop_ok
    if verified:
        model.consume_window_restore()
    return {
        "verified": verified,
        "restored_action": restore["action"],
        "expected": previous,
        "window": actual,
    }


async def desktop_close_window(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    window_id = str(arguments["window_id"])
    await _desktop(context).bridge.request("close", {"window_id": window_id})
    actual = None
    for _ in range(12):
        actual = await _window_after(context, window_id, 0.15)
        if actual is None:
            break
    return {
        "verified": actual is None,
        "closed": actual is None,
        "window_id": window_id,
        "remaining_window": actual,
    }


def _security(context: ToolContext) -> Any:
    if context.security_center is None:
        raise RuntimeError("The E.V. security center is unavailable")
    return context.security_center


def security_overview(_arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return _security(context).overview()


def security_firewall(_arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return _security(context).firewall()


def security_network(_arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return _security(context).sockets()


def security_ssh(_arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return _security(context).ssh()


def security_startup(_arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return _security(context).startup()


def security_logins(_arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return _security(context).login_activity()


def security_ev(_arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return _security(context).ev_security()


def security_updates(_arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return _security(context).updates(refresh=True)


def coding_agent_status(_arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    if context.coding_agent is None:
        raise RuntimeError("The coding-agent gateway is unavailable")
    return context.coding_agent.status()


def coding_agent_proposal(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    if context.coding_agent is None:
        raise RuntimeError("The coding-agent gateway is unavailable")
    return context.coding_agent.propose(
        str(arguments["request"]),
        str(arguments["project"]),
        str(arguments.get("diagnostics", "")),
    )


def coding_agent_execute(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    if context.coding_agent is None:
        raise RuntimeError("The coding-agent gateway is unavailable")
    return context.coding_agent.execute(
        str(arguments["proposal_id"]),
        int(arguments.get("timeout_seconds", 1200)),
    )


def coding_agent_result(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    if context.coding_agent is None:
        raise RuntimeError("The coding-agent gateway is unavailable")
    return context.coding_agent.result(str(arguments["proposal_id"]))


def _accessibility(context: ToolContext) -> Any:
    if context.accessibility is None:
        raise RuntimeError("The AT-SPI accessibility bridge is unavailable")
    return context.accessibility


def accessibility_status(_arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return _accessibility(context).status()


def accessibility_enable(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return _accessibility(context).enable_session(bool(arguments["enabled"]))


def accessibility_list(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return _accessibility(context).list_elements(
        str(arguments.get("application", "")),
        str(arguments.get("query", "")),
        str(arguments.get("role", "")),
        int(arguments.get("limit", 100)),
    )


async def _active_accessibility_scope(
    arguments: dict[str, Any], context: ToolContext
) -> tuple[int, str]:
    window_id = str(arguments["window_id"])
    world = await _desktop(context).bridge.request("snapshot", {}, timeout=4)
    window = next(
        (item for item in world.get("windows", []) if str(item.get("id")) == window_id), None
    )
    if window is None or window.get("minimized") or window.get("special"):
        raise RuntimeError("The exact accessibility target window is unavailable")
    if str(world.get("active_window_id", "")) != window_id:
        active = next(
            (
                w
                for w in world.get("windows", [])
                if str(w.get("id")) == str(world.get("active_window_id", ""))
            ),
            {},
        )
        raise RuntimeError(
            f"The exact accessibility target window lost focus (target={window_id}, active={world.get('active_window_id', '')}, active_app={active.get('app_id', '')}, active_pid={active.get('pid', '')})"
        )
    process_id = int(window.get("pid", 0))
    title = str(window.get("title", "")).strip()
    if process_id <= 0 or not title:
        raise RuntimeError(
            "KWin did not expose enough identity to bind the accessibility target safely"
        )
    return process_id, title


async def accessibility_activate(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    process_id, title = await _active_accessibility_scope(arguments, context)
    return await asyncio.to_thread(
        _accessibility(context).activate,
        str(arguments["application"]),
        str(arguments["name"]),
        str(arguments.get("role", "")),
        process_id,
        title,
    )


async def accessibility_set_text(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    process_id, title = await _active_accessibility_scope(arguments, context)
    return await asyncio.to_thread(
        _accessibility(context).set_text,
        str(arguments["application"]),
        str(arguments["name"]),
        str(arguments["text"]),
        str(arguments.get("role", "")),
        process_id,
        title,
    )


def _vision(context: ToolContext) -> Any:
    if context.vision is None:
        raise RuntimeError("The on-demand screen-perception service is unavailable")
    return context.vision


def vision_status(_arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return _vision(context).status()


def desktop_input_status(_arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return _desktop(context).input.status()


async def desktop_input_connect(_arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return await _desktop(context).input.connect()


async def desktop_input_disconnect(
    _arguments: dict[str, Any], context: ToolContext
) -> dict[str, Any]:
    return await _desktop(context).input.close()


async def desktop_pointer_move(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return await _desktop(context).input.move(
        str(arguments["window_id"]), float(arguments["x"]), float(arguments["y"])
    )


async def desktop_pointer_click(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return await _desktop(context).input.click(
        str(arguments["window_id"]),
        float(arguments["x"]),
        float(arguments["y"]),
        str(arguments.get("button", "left")),
        int(arguments.get("count", 1)),
    )


async def desktop_pointer_scroll(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return await _desktop(context).input.scroll(
        str(arguments["window_id"]),
        float(arguments["x"]),
        float(arguments["y"]),
        str(arguments["direction"]),
        int(arguments.get("steps", 3)),
    )


async def desktop_keyboard_key(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return await _desktop(context).input.key(
        str(arguments["window_id"]), str(arguments["key"]), list(arguments.get("modifiers", []))
    )


async def desktop_keyboard_type_text(
    arguments: dict[str, Any], context: ToolContext
) -> dict[str, Any]:
    return await _desktop(context).input.type_text(
        str(arguments["window_id"]), str(arguments["text"])
    )


async def vision_capture(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return await _vision(context).capture(
        str(arguments.get("output", "")), str(arguments.get("window_id", ""))
    )


def vision_delete(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return _vision(context).delete(str(arguments["capture_id"]))


def vision_ocr(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return _vision(context).ocr(
        str(arguments["capture_id"]),
        float(arguments.get("minimum_score", 0.55)),
    )


def memory_search(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    if context.memory is None:
        raise RuntimeError("memory store is unavailable")
    return {
        "memories": context.memory.list_memories(
            arguments.get("query", ""), int(arguments.get("limit", 20))
        )
    }


def memory_remember(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    if context.memory is None:
        raise RuntimeError("memory store is unavailable")
    return {"memory": context.memory.remember(arguments["content"], arguments.get("tags", []))}


def memory_forget(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    if context.memory is None:
        raise RuntimeError("memory store is unavailable")
    return {"removed": context.memory.forget(arguments["id"]), "id": arguments["id"]}


def get_clock(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    now = datetime.now().astimezone()
    return {
        "iso8601": now.isoformat(),
        "local_time": now.strftime("%I:%M %p").lstrip("0"),
        "local_date": now.strftime("%A, %B %d, %Y"),
        "timezone": now.tzname(),
        "source": "operating_system_clock",
    }


def register_builtin_tools(registry: ToolRegistry) -> None:
    register = registry.register
    register(
        ToolSpec(
            "system.clock",
            "SYSTEM",
            "Read the computer's current local time, date and timezone. Does not change the clock.",
            Permission.SAFE,
            EMPTY_SCHEMA,
            get_clock,
        )
    )
    register(
        ToolSpec(
            "system.get_cpu_usage",
            "SYSTEM",
            "Read current CPU utilization, load, and frequency.",
            Permission.SAFE,
            EMPTY_SCHEMA,
            get_cpu_usage,
        )
    )
    register(
        ToolSpec(
            "system.get_memory_usage",
            "SYSTEM",
            "Read current RAM and swap utilization.",
            Permission.SAFE,
            EMPTY_SCHEMA,
            get_memory_usage,
        )
    )
    register(
        ToolSpec(
            "system.get_temperature",
            "SYSTEM",
            "Read the CPU package temperature from Linux hwmon.",
            Permission.SAFE,
            EMPTY_SCHEMA,
            get_temperature,
        )
    )
    register(
        ToolSpec(
            "system.get_disk_usage",
            "SYSTEM",
            "Read disk capacity for root or an allowed path.",
            Permission.SAFE,
            object_schema({"path": {"type": "string", "maxLength": 4096}}),
            get_disk_usage,
        )
    )
    register(
        ToolSpec(
            "system.get_processes",
            "SYSTEM",
            "List current processes sorted by memory or CPU.",
            Permission.SAFE,
            object_schema(
                {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    "sort": {"type": "string", "enum": ["memory", "cpu"]},
                    "query": {"type": "string", "maxLength": 128},
                }
            ),
            get_processes,
        )
    )
    register(
        ToolSpec(
            "system.get_network_status",
            "SYSTEM",
            "Read interface state and aggregate byte counters.",
            Permission.SAFE,
            EMPTY_SCHEMA,
            get_network_status,
        )
    )
    register(
        ToolSpec(
            "system.get_battery",
            "SYSTEM",
            "Read battery percentage and charging state.",
            Permission.SAFE,
            EMPTY_SCHEMA,
            get_battery,
        )
    )
    register(
        ToolSpec(
            "system.identity",
            "SYSTEM",
            "Read the current Gentoo kernel, architecture, hostname, desktop, and session type.",
            Permission.SAFE,
            EMPTY_SCHEMA,
            get_system_identity,
        )
    )
    register(
        ToolSpec(
            "system.mounts",
            "SYSTEM",
            "List mounted filesystems with real capacity and free-space observations.",
            Permission.SAFE,
            EMPTY_SCHEMA,
            get_mounts,
        )
    )
    register(
        ToolSpec(
            "system.devices",
            "SYSTEM",
            "List currently visible USB, connected Bluetooth, and PipeWire audio devices.",
            Permission.SAFE,
            EMPTY_SCHEMA,
            get_connected_devices,
            timeout_seconds=20,
        )
    )
    register(
        ToolSpec(
            "system.openrc_services",
            "SYSTEM",
            "Read all OpenRC service states and runlevel membership without changing services.",
            Permission.SAFE,
            EMPTY_SCHEMA,
            get_openrc_services,
        )
    )

    app_schema = object_schema(
        {"desktop_id": {"type": "string", "minLength": 1, "maxLength": 200}}, ["desktop_id"]
    )
    register(
        ToolSpec(
            "applications.list",
            "APPLICATIONS",
            "List trusted installed desktop entries; launch_only skips live window/process enrichment.",
            Permission.SAFE,
            object_schema(
                {
                    "query": {"type": "string", "maxLength": 128},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    "launch_only": {"type": "boolean"},
                }
            ),
            list_applications,
        )
    )
    register(
        ToolSpec(
            "applications.open",
            "APPLICATIONS",
            "Open an installed application by desktop-entry ID.",
            Permission.SAFE,
            app_schema,
            open_application,
        )
    )
    register(
        ToolSpec(
            "applications.focus",
            "APPLICATIONS",
            "Request activation of an installed application's desktop entry.",
            Permission.SAFE,
            app_schema,
            focus_application,
        )
    )
    register(
        ToolSpec(
            "applications.close_process",
            "APPLICATIONS",
            "Gracefully close an exactly revalidated user application process.",
            Permission.SENSITIVE,
            object_schema(
                {
                    "pid": {"type": "integer", "minimum": 2},
                    "started_at_epoch": {"type": "number", "minimum": 0},
                    "expected_query": {"type": "string", "minLength": 1, "maxLength": 128},
                },
                ["pid", "started_at_epoch", "expected_query"],
            ),
            close_process,
            confirmation_reason="Closing an application may discard unsaved work.",
        )
    )

    register(
        ToolSpec(
            "audio.get_volume",
            "AUDIO",
            "Read default output volume and mute state.",
            Permission.SAFE,
            EMPTY_SCHEMA,
            get_volume,
        )
    )
    register(
        ToolSpec(
            "audio.set_volume",
            "AUDIO",
            "Set normal output volume from 0 to 100 percent.",
            Permission.SAFE,
            object_schema(
                {"percent": {"type": "integer", "minimum": 0, "maximum": 100}}, ["percent"]
            ),
            set_volume,
        )
    )
    register(
        ToolSpec(
            "audio.adjust_volume",
            "AUDIO",
            "Adjust the current output volume by a relative number of percentage points, clamp to 0-100 and read back the exact output state.",
            Permission.LOW_RISK,
            object_schema(
                {"delta": {"type": "integer", "minimum": -100, "maximum": 100}}, ["delta"]
            ),
            adjust_volume,
            cancellable=False,
        )
    )
    register(
        ToolSpec(
            "audio.set_mute",
            "AUDIO",
            "Mute or unmute the default output.",
            Permission.SAFE,
            object_schema({"muted": {"type": "boolean"}}, ["muted"]),
            set_mute,
        )
    )
    register(
        ToolSpec(
            "audio.media",
            "AUDIO",
            "Control existing playback through local MPRIS and verify player state. Resume/unpause music with action play; suspend with pause. For Spotify use player spotify. This keeps the current track/queue and needs no Spotify API account. Omit player to resolve current playback.",
            Permission.SAFE,
            object_schema(
                {
                    "action": {
                        "type": "string",
                        "enum": ["play", "pause", "toggle", "next", "previous"],
                    },
                    "player": {"type": "string", "maxLength": 100},
                },
                ["action"],
            ),
            control_media,
            timeout_seconds=20,
        )
    )
    register(
        ToolSpec(
            "audio.devices",
            "AUDIO",
            "List real PipeWire input/output devices and identify both system defaults.",
            Permission.SAFE,
            EMPTY_SCHEMA,
            get_audio_devices,
            verification="pactl JSON observation",
            side_effects=(),
        )
    )
    register(
        ToolSpec(
            "audio.default_input.set",
            "AUDIO",
            "Set one exact non-monitor PipeWire input as the system default and verify it.",
            Permission.LOW_RISK,
            object_schema(
                {"source": {"type": "string", "minLength": 1, "maxLength": 300}}, ["source"]
            ),
            set_default_audio_input,
            verification="Read back default source",
            side_effects=("changes the default microphone",),
        )
    )
    register(
        ToolSpec(
            "audio.default_output.set",
            "AUDIO",
            "Set one exact PipeWire output as the system default and verify it.",
            Permission.LOW_RISK,
            object_schema({"sink": {"type": "string", "minLength": 1, "maxLength": 300}}, ["sink"]),
            set_default_audio_output,
            verification="Read back default sink",
            side_effects=("changes the default audio output",),
        )
    )
    register(
        ToolSpec(
            "audio.microphone_mute.set",
            "AUDIO",
            "Mute or unmute the default or an exact available microphone and verify it.",
            Permission.LOW_RISK,
            object_schema(
                {"muted": {"type": "boolean"}, "source": {"type": "string", "maxLength": 300}},
                ["muted"],
            ),
            set_microphone_mute,
            verification="Read back source mute state",
            side_effects=("changes microphone mute state",),
        )
    )

    root_property = {"type": "string", "maxLength": 4096}
    register(
        ToolSpec(
            "files.find",
            "FILES",
            "Find files by name under an allowed root.",
            Permission.SAFE,
            object_schema(
                {
                    "query": {"type": "string", "minLength": 1, "maxLength": 256},
                    "root": root_property,
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    "include_hidden": {"type": "boolean"},
                },
                ["query"],
            ),
            find_file,
            timeout_seconds=40,
        )
    )
    register(
        ToolSpec(
            "files.info",
            "FILES",
            "Read metadata for an allowed path.",
            Permission.SAFE,
            object_schema({"path": root_property}, ["path"]),
            get_file_info,
        )
    )
    register(
        ToolSpec(
            "files.list",
            "FILES",
            "List one allowed directory with bounded metadata and no recursive scan.",
            Permission.SAFE,
            object_schema(
                {
                    "path": root_property,
                    "limit": {"type": "integer", "minimum": 1, "maximum": 250},
                    "include_hidden": {"type": "boolean"},
                },
                ["path"],
            ),
            list_directory,
        )
    )
    register(
        ToolSpec(
            "files.open",
            "FILES",
            "Open an allowed file with its desktop default application.",
            Permission.SAFE,
            object_schema({"path": root_property}, ["path"]),
            open_file,
        )
    )
    register(
        ToolSpec(
            "files.read",
            "FILES",
            "Read a size-limited text file from an allowed root.",
            Permission.SENSITIVE,
            object_schema(
                {
                    "path": root_property,
                    "max_bytes": {"type": "integer", "minimum": 1, "maximum": 65536},
                },
                ["path"],
            ),
            read_file,
            normalizer=normalize_path_argument("path"),
            confirmation_reason="File contents may be private and will be shared with the active reasoning request.",
        )
    )
    register(
        ToolSpec(
            "files.directory.create",
            "FILES",
            "Create one exact directory under an allowed root without replacing anything. Create a folder.",
            Permission.LOW_RISK,
            object_schema({"path": root_property, "parents": {"type": "boolean"}}, ["path"]),
            create_directory,
            verification="Requested directory exists",
            side_effects=("creates one or more directories",),
        )
    )
    register(
        ToolSpec(
            "files.text.create",
            "FILES",
            "Create one private UTF-8 text file under an allowed root; never overwrite an existing path. Write exact file contents.",
            Permission.LOW_RISK,
            object_schema(
                {"path": root_property, "content": {"type": "string", "maxLength": 65536}},
                ["path", "content"],
            ),
            create_text_file,
            verification="Read back exact UTF-8 content",
            side_effects=("creates one file",),
        )
    )
    register(
        ToolSpec(
            "files.hash",
            "FILES",
            "Compute the SHA-256 and byte size of one allowed regular file.",
            Permission.SAFE,
            object_schema({"path": root_property}, ["path"]),
            hash_file,
            verification="Stream the complete file into SHA-256",
            side_effects=(),
        )
    )
    transfer_schema = object_schema(
        {"source": root_property, "destination": root_property}, ["source", "destination"]
    )
    register(
        ToolSpec(
            "files.copy",
            "FILES",
            "Copy one exact file or directory between allowed paths without following a source symlink or overwriting the destination.",
            Permission.SENSITIVE,
            transfer_schema,
            copy_path,
            verification="Destination existence and SHA-256 for regular files",
            side_effects=("creates a file or directory tree",),
            timeout_seconds=120,
        )
    )
    register(
        ToolSpec(
            "files.move",
            "FILES",
            "Move or rename one exact allowed path without overwriting the destination.",
            Permission.SENSITIVE,
            transfer_schema,
            move_path,
            verification="Destination exists and source no longer exists",
            side_effects=("moves or renames a file or directory",),
            timeout_seconds=120,
        )
    )
    register(
        ToolSpec(
            "files.trash",
            "FILES",
            "Move one exact allowed file or directory to the desktop trash; never remove an allowed root.",
            Permission.SENSITIVE,
            object_schema({"path": root_property}, ["path"]),
            trash_path,
            verification="Original path no longer exists after gio trash",
            side_effects=("moves one path to recoverable desktop trash",),
            timeout_seconds=40,
        )
    )

    project_search = object_schema(
        {
            "query": {"type": "string", "maxLength": 256},
            "root": root_property,
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        }
    )
    register(
        ToolSpec(
            "development.find_project",
            "DEVELOPMENT",
            "Discover project roots using known build and source-control markers.",
            Permission.SAFE,
            project_search,
            find_project,
            timeout_seconds=40,
        )
    )
    register(
        ToolSpec(
            "development.git_status",
            "DEVELOPMENT",
            "Read concise Git status for an allowed project.",
            Permission.SAFE,
            object_schema({"project": root_property}, ["project"]),
            get_git_status,
        )
    )
    register(
        ToolSpec(
            "development.build_project",
            "DEVELOPMENT",
            "Retired compatibility endpoint: does not execute code. Use development.project.inspect then approval-gated development.project.run instead.",
            Permission.SAFE,
            object_schema(
                {
                    "project": root_property,
                    "target": {"type": "string", "maxLength": 80},
                    "timeout_seconds": {"type": "integer", "minimum": 10, "maximum": 900},
                },
                ["project"],
            ),
            build_project,
            read_only=True,
        )
    )
    register(
        ToolSpec(
            "development.inspect_build_error",
            "DEVELOPMENT",
            "Extract likely error locations from supplied build output.",
            Permission.SAFE,
            object_schema(
                {"text": {"type": "string", "minLength": 1, "maxLength": 200000}}, ["text"]
            ),
            inspect_build_error,
        )
    )

    register(
        ToolSpec(
            "desktop.send_notification",
            "DESKTOP",
            "Show a normal E.V. desktop notification.",
            Permission.SAFE,
            object_schema(
                {
                    "title": {"type": "string", "minLength": 1, "maxLength": 100},
                    "message": {"type": "string", "maxLength": 1000},
                },
                ["title", "message"],
            ),
            send_notification,
        )
    )
    register(
        ToolSpec(
            "desktop.clipboard_read",
            "DESKTOP",
            "Read the current KDE clipboard text.",
            Permission.SENSITIVE,
            EMPTY_SCHEMA,
            clipboard_read,
            confirmation_reason="Clipboard contents may contain private information.",
        )
    )
    register(
        ToolSpec(
            "desktop.clipboard_write",
            "DESKTOP",
            "Replace the current KDE clipboard text.",
            Permission.SENSITIVE,
            object_schema({"content": {"type": "string", "maxLength": 65536}}, ["content"]),
            clipboard_write,
            confirmation_reason="This replaces the current clipboard contents.",
        )
    )
    register(
        ToolSpec(
            "desktop.window_information",
            "DESKTOP",
            "Read the active output and virtual desktop from KWin.",
            Permission.SAFE,
            EMPTY_SCHEMA,
            window_information,
        )
    )

    window_id = {"type": "string", "minLength": 8, "maxLength": 80}
    window_query = object_schema(
        {
            "query": {"type": "string", "maxLength": 256},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        }
    )
    resolver = object_schema(
        {"description": {"type": "string", "minLength": 1, "maxLength": 256}}, ["description"]
    )
    geometry = {
        "window_id": window_id,
        "x": {"type": "integer", "minimum": -32768, "maximum": 32768},
        "y": {"type": "integer", "minimum": -32768, "maximum": 32768},
        "width": {"type": "integer", "minimum": 64, "maximum": 32768},
        "height": {"type": "integer", "minimum": 64, "maximum": 32768},
    }
    kwin_requirements = ("KDE Plasma 6", "Wayland", "KWin scripting", "same-user session D-Bus")
    register(
        ToolSpec(
            "desktop.world",
            "DESKTOP",
            "Read the current KWin windows, monitors, workspaces, focus, and cursor state.",
            Permission.SAFE,
            EMPTY_SCHEMA,
            desktop_world,
            platform_requirements=kwin_requirements,
            verification="Fresh KWin snapshot",
            side_effects=(),
            expected_latency_ms=80,
        )
    )
    register(
        ToolSpec(
            "desktop.windows.list",
            "DESKTOP",
            "List or semantically resolve real KWin windows.",
            Permission.SAFE,
            window_query,
            desktop_list_windows,
            platform_requirements=kwin_requirements,
            verification="Fresh KWin snapshot",
            side_effects=(),
            expected_latency_ms=80,
        )
    )
    register(
        ToolSpec(
            "desktop.window.resolve",
            "DESKTOP",
            "Resolve a natural window description without guessing ambiguous targets, optionally excluding the previously selected window.",
            Permission.SAFE,
            object_schema(
                {
                    "description": {"type": "string", "minLength": 1, "maxLength": 500},
                    "exclude_window_id": window_id,
                },
                ["description"],
            ),
            desktop_resolve_window,
            platform_requirements=kwin_requirements,
            verification="Exact KWin internal window ID",
            side_effects=(),
            expected_latency_ms=80,
        )
    )
    register(
        ToolSpec(
            "desktop.window.wait",
            "DESKTOP",
            "Wait a bounded time for an application to expose a matching KWin window.",
            Permission.SAFE,
            object_schema(
                {
                    "description": {"type": "string", "minLength": 1, "maxLength": 256},
                    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 30},
                },
                ["description"],
            ),
            desktop_wait_for_window,
            platform_requirements=kwin_requirements,
            verification="Fresh KWin snapshot contains one resolved window",
            side_effects=(),
            expected_latency_ms=500,
        )
    )
    register(
        ToolSpec(
            "desktop.output.resolve",
            "DESKTOP",
            "Resolve main, second, other, laptop, external, left, or right monitor from live topology.",
            Permission.SAFE,
            resolver,
            desktop_resolve_output,
            platform_requirements=kwin_requirements,
            verification="Exact KWin output name",
            side_effects=(),
            expected_latency_ms=80,
        )
    )
    register(
        ToolSpec(
            "desktop.workspace.resolve",
            "DESKTOP",
            "Resolve a current, numbered, or named KWin virtual desktop without guessing.",
            Permission.SAFE,
            resolver,
            desktop_resolve_workspace,
            platform_requirements=kwin_requirements,
            verification="Exact KWin virtual desktop ID",
            side_effects=(),
            expected_latency_ms=80,
        )
    )
    register(
        ToolSpec(
            "desktop.window.activate",
            "DESKTOP",
            "Focus and raise one exact KWin window, then verify focus.",
            Permission.LOW_RISK,
            object_schema({"window_id": window_id}, ["window_id"]),
            desktop_activate_window,
            platform_requirements=kwin_requirements,
            verification="Re-read active KWin window",
            side_effects=("changes focus", "restores minimized window"),
            expected_latency_ms=250,
        )
    )
    input_requirements = (*kwin_requirements, "KDE RemoteDesktop portal session")
    pointer_position = {
        "window_id": window_id,
        "x": {"type": "number", "minimum": -32768, "maximum": 32768},
        "y": {"type": "number", "minimum": -32768, "maximum": 32768},
    }
    register(
        ToolSpec(
            "desktop.input.status",
            "DESKTOP",
            "Report whether the session-scoped KDE pointer and keyboard portal is connected.",
            Permission.SAFE,
            EMPTY_SCHEMA,
            desktop_input_status,
            expected_latency_ms=5,
        )
    )
    register(
        ToolSpec(
            "desktop.input.connect",
            "DESKTOP",
            "Start local pointer and keyboard control using the native KDE desktop permission dialog; no root daemon or network listener.",
            Permission.SENSITIVE,
            EMPTY_SCHEMA,
            desktop_input_connect,
            timeout_seconds=75,
            platform_requirements=input_requirements,
            verification="KDE portal returns granted input-device bits",
            side_effects=("may display KDE's native session permission dialog",),
            expected_latency_ms=1000,
        )
    )
    register(
        ToolSpec(
            "desktop.input.disconnect",
            "DESKTOP",
            "Immediately close E.V.'s compositor input session.",
            Permission.LOW_RISK,
            EMPTY_SCHEMA,
            desktop_input_disconnect,
            expected_latency_ms=50,
        )
    )
    register(
        ToolSpec(
            "desktop.pointer.move",
            "DESKTOP",
            "Move the pointer to global logical desktop coordinates inside one exact focused window and verify KWin's cursor position.",
            Permission.LOW_RISK,
            object_schema(pointer_position, list(pointer_position)),
            desktop_pointer_move,
            platform_requirements=input_requirements,
            verification="Live KWin cursor is within one logical pixel of the target",
            side_effects=("moves the pointer",),
            expected_latency_ms=200,
        )
    )
    register(
        ToolSpec(
            "desktop.pointer.click",
            "DESKTOP",
            "Click current visible global logical coordinates inside an exact focused unobscured window. Follow with screenshot or accessibility inspection to verify the app outcome.",
            Permission.SENSITIVE,
            object_schema(
                {
                    **pointer_position,
                    "button": {"type": "string", "enum": ["left", "middle", "right"]},
                    "count": {"type": "integer", "minimum": 1, "maximum": 2},
                },
                list(pointer_position),
            ),
            desktop_pointer_click,
            platform_requirements=input_requirements,
            verification="Focus and pointer checked before input; application outcome requires follow-up observation",
            side_effects=("clicks an application control",),
            expected_latency_ms=300,
        )
    )
    register(
        ToolSpec(
            "desktop.pointer.scroll",
            "DESKTOP",
            "Scroll 1-20 wheel steps at visible global logical coordinates inside an exact focused window; inspect the result afterward.",
            Permission.LOW_RISK,
            object_schema(
                {
                    **pointer_position,
                    "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                    "steps": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                [*pointer_position, "direction"],
            ),
            desktop_pointer_scroll,
            platform_requirements=input_requirements,
            verification="Input delivery; verify changed content with perception",
            side_effects=("scrolls application content",),
            expected_latency_ms=250,
        )
    )
    register(
        ToolSpec(
            "desktop.keyboard.key",
            "DESKTOP",
            "Send an explicit key or shortcut such as Ctrl+L or Enter to one exact focused window; modifiers are released even on failure.",
            Permission.SENSITIVE,
            object_schema(
                {
                    "window_id": window_id,
                    "key": {"type": "string", "minLength": 1, "maxLength": 20},
                    "modifiers": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["ctrl", "control", "shift", "alt", "super", "meta"],
                        },
                        "maxItems": 4,
                    },
                },
                ["window_id", "key"],
            ),
            desktop_keyboard_key,
            platform_requirements=input_requirements,
            verification="Input delivery only; observe the app to verify the requested change",
            side_effects=("sends an application key or shortcut",),
            expected_latency_ms=150,
        )
    )
    register(
        ToolSpec(
            "desktop.keyboard.type_text",
            "DESKTOP",
            "Type printable text into one exact focused window without submitting or changing the clipboard. Prefer AT-SPI set_text for a named editable field and verify text afterward.",
            Permission.SENSITIVE,
            object_schema(
                {
                    "window_id": window_id,
                    "text": {"type": "string", "minLength": 1, "maxLength": 2000},
                },
                ["window_id", "text"],
            ),
            desktop_keyboard_type_text,
            timeout_seconds=60,
            platform_requirements=input_requirements,
            verification="Input delivery with repeated focus checks; use AT-SPI or OCR readback for text verification",
            side_effects=("types into the focused field",),
            expected_latency_ms=400,
        )
    )
    register(
        ToolSpec(
            "desktop.window.move_resize",
            "DESKTOP",
            "Set exact frame geometry for one KWin window, then verify actual geometry.",
            Permission.LOW_RISK,
            object_schema(geometry, list(geometry)),
            desktop_move_resize_window,
            platform_requirements=kwin_requirements,
            verification="Re-read and compare frame geometry",
            side_effects=(
                "moves window",
                "resizes window",
                "restores fullscreen/maximized state",
                "may focus the window while restoring it",
            ),
            expected_latency_ms=500,
        )
    )
    register(
        ToolSpec(
            "desktop.window.state",
            "DESKTOP",
            "Minimize, maximize, restore, or fullscreen one exact KWin window and verify it.",
            Permission.LOW_RISK,
            object_schema(
                {
                    "window_id": window_id,
                    "state": {
                        "type": "string",
                        "enum": ["minimize", "maximize", "restore", "fullscreen"],
                    },
                },
                ["window_id", "state"],
            ),
            desktop_set_window_state,
            platform_requirements=kwin_requirements,
            verification="Re-read KWin window state",
            side_effects=("changes window state",),
            expected_latency_ms=350,
        )
    )
    register(
        ToolSpec(
            "desktop.window.move_to_output",
            "DESKTOP",
            "Move one exact KWin window to a named output and verify placement.",
            Permission.LOW_RISK,
            object_schema(
                {
                    "window_id": window_id,
                    "output": {"type": "string", "minLength": 1, "maxLength": 100},
                },
                ["window_id", "output"],
            ),
            desktop_move_window_to_output,
            platform_requirements=kwin_requirements,
            verification="Re-read KWin output assignment",
            side_effects=(
                "moves window to another output",
                "preserves KWin maximize/tile semantics",
            ),
            expected_latency_ms=350,
        )
    )
    register(
        ToolSpec(
            "desktop.window.move_to_workspace",
            "DESKTOP",
            "Move one exact KWin window to an existing virtual desktop and verify assignment.",
            Permission.LOW_RISK,
            object_schema(
                {
                    "window_id": window_id,
                    "desktop_id": {"type": "string", "minLength": 1, "maxLength": 100},
                },
                ["window_id", "desktop_id"],
            ),
            desktop_move_window_to_workspace,
            platform_requirements=kwin_requirements,
            verification="Re-read KWin virtual desktop assignment",
            side_effects=("moves window to another virtual desktop",),
            expected_latency_ms=350,
        )
    )
    register(
        ToolSpec(
            "desktop.window.layout",
            "DESKTOP",
            "Center or place one exact KWin window on a screen half or corner quarter using the panel-aware work area.",
            Permission.LOW_RISK,
            object_schema(
                {
                    "window_id": window_id,
                    "layout": {
                        "type": "string",
                        "enum": [
                            "center",
                            "left",
                            "right",
                            "top",
                            "bottom",
                            "top-left",
                            "top-right",
                            "bottom-left",
                            "bottom-right",
                        ],
                    },
                },
                ["window_id", "layout"],
            ),
            desktop_layout_window,
            platform_requirements=kwin_requirements,
            verification="Compare actual frame geometry with KWin client-area target",
            side_effects=(
                "moves and resizes a window",
                "restores fullscreen, maximized, or tiled state",
            ),
            expected_latency_ms=400,
        )
    )
    register(
        ToolSpec(
            "desktop.window.undo_last",
            "DESKTOP",
            "Undo the most recent E.V. window move, resize, layout, output, workspace, or state change.",
            Permission.LOW_RISK,
            EMPTY_SCHEMA,
            desktop_undo_window_change,
            platform_requirements=kwin_requirements,
            verification="Restore and re-read geometry, output, workspace, and state",
            side_effects=("restores the prior state of one window",),
            expected_latency_ms=900,
        )
    )
    register(
        ToolSpec(
            "desktop.window.close",
            "DESKTOP",
            "Request a graceful close for one exact KWin window and verify it disappeared.",
            Permission.SENSITIVE,
            object_schema({"window_id": window_id}, ["window_id"]),
            desktop_close_window,
            confirmation_reason="Closing a window may discard unsaved work.",
            platform_requirements=kwin_requirements,
            verification="Poll KWin until the exact window ID disappears",
            side_effects=("closes a window", "may discard unsaved work"),
            expected_latency_ms=1200,
        )
    )

    security_read = {
        "platform_requirements": ("Gentoo Linux", "OpenRC", "procfs/sysfs"),
        "verification": "Return command source and confidence for each observation",
        "side_effects": (),
        "expected_latency_ms": 350,
    }
    register(
        ToolSpec(
            "security.overview",
            "SECURITY",
            "Run a local read-only security overview with evidence and confidence.",
            Permission.SAFE,
            EMPTY_SCHEMA,
            security_overview,
            **security_read,
        )
    )
    register(
        ToolSpec(
            "security.firewall",
            "SECURITY",
            "Inspect active firewall implementations and readable rule state without changing it.",
            Permission.SAFE,
            EMPTY_SCHEMA,
            security_firewall,
            **security_read,
        )
    )
    register(
        ToolSpec(
            "security.network_exposure",
            "SECURITY",
            "Inspect real listening sockets, binding scope, owning process visibility, and connection counts.",
            Permission.SAFE,
            EMPTY_SCHEMA,
            security_network,
            **security_read,
        )
    )
    register(
        ToolSpec(
            "security.ssh",
            "SECURITY",
            "Inspect OpenRC SSH daemon state and readable configured port.",
            Permission.SAFE,
            EMPTY_SCHEMA,
            security_ssh,
            **security_read,
        )
    )
    register(
        ToolSpec(
            "security.startup",
            "SECURITY",
            "Inspect KDE/XDG autostart and user shell startup files.",
            Permission.SAFE,
            EMPTY_SCHEMA,
            security_startup,
            **security_read,
        )
    )
    register(
        ToolSpec(
            "security.login_activity",
            "SECURITY",
            "Inspect recent local login-session records.",
            Permission.SAFE,
            EMPTY_SCHEMA,
            security_logins,
            **security_read,
        )
    )
    register(
        ToolSpec(
            "security.ev",
            "SECURITY",
            "Audit E.V. IPC, config, database, credential-file, and runtime permissions.",
            Permission.SAFE,
            EMPTY_SCHEMA,
            security_ev,
            **security_read,
        )
    )
    register(
        ToolSpec(
            "security.updates",
            "SECURITY",
            "Explicitly run the potentially slow Gentoo GLSA advisory check and cache its evidence.",
            Permission.SAFE,
            EMPTY_SCHEMA,
            security_updates,
            platform_requirements=("Gentoo Linux", "glsa-check"),
            verification="Return glsa-check evidence and explicit limitations",
            side_effects=(),
            expected_latency_ms=15000,
        )
    )
    register(
        ToolSpec(
            "development.coding_agent_status",
            "DEVELOPMENT",
            "Report the real availability and safety mode of the controlled coding-agent gateway.",
            Permission.SAFE,
            EMPTY_SCHEMA,
            coding_agent_status,
            verification="Executable discovery and gateway policy",
            side_effects=(),
            expected_latency_ms=20,
        )
    )
    proposal_id = {"type": "string", "pattern": "^[0-9a-f]{32}$", "maxLength": 32}
    register(
        ToolSpec(
            "development.coding_agent_propose",
            "DEVELOPMENT",
            "Prepare a read-only coding task proposal for later approval-gated execution without editing or running project code.",
            Permission.SAFE,
            object_schema(
                {
                    "request": {"type": "string", "minLength": 1, "maxLength": 4000},
                    "project": root_property,
                    "diagnostics": {"type": "string", "maxLength": 20000},
                },
                ["request", "project"],
            ),
            coding_agent_proposal,
            normalizer=normalize_path_argument("project"),
            verification="Project allowlist, saved login, and clean Git checkpoint readiness",
            side_effects=("writes private E.V. task metadata",),
            expected_latency_ms=250,
        )
    )
    register(
        ToolSpec(
            "development.coding_agent_execute",
            "DEVELOPMENT",
            "Run one approved proposal through Codex in an isolated Git worktree, validate it, and commit only a passing review branch.",
            Permission.HIGH,
            object_schema(
                {
                    "proposal_id": proposal_id,
                    "timeout_seconds": {"type": "integer", "minimum": 60, "maximum": 1800},
                },
                ["proposal_id"],
            ),
            coding_agent_execute,
            confirmation_reason="Codex will be allowed to edit and run project code in an isolated Git worktree. The live checkout will not be changed or deployed.",
            timeout_seconds=1810,
            verification="Clean checkpoint, isolated worktree, fixed tests, changed-file and risk manifests, review commit",
            side_effects=(
                "creates a private Git worktree and branch",
                "runs project code in the workspace-write sandbox",
                "connects to the Codex service",
                "creates a review commit only after validation",
            ),
            expected_latency_ms=120000,
            requires_confirmation=True,
        )
    )
    register(
        ToolSpec(
            "development.coding_agent_cancel",
            "DEVELOPMENT",
            "Cancel one running isolated Carlos Engineering job. Completion is reported separately; existing edits remain in its private worktree.",
            Permission.LOW_RISK,
            object_schema({"proposal_id": proposal_id}, ["proposal_id"]),
            lambda a, c: c.coding_agent.cancel(a["proposal_id"]),
            offline_available=True,
            reversible=False,
        )
    )
    register(
        ToolSpec(
            "development.coding_agent_result",
            "DEVELOPMENT",
            "Read the stored status, diff summary, validation results, and review commit for one coding task.",
            Permission.SAFE,
            object_schema({"proposal_id": proposal_id}, ["proposal_id"]),
            coding_agent_result,
            verification="Read private persisted task metadata",
            side_effects=(),
            expected_latency_ms=30,
        )
    )

    accessibility_locator = {
        "application": {"type": "string", "minLength": 1, "maxLength": 200},
        "name": {"type": "string", "minLength": 1, "maxLength": 500},
        "role": {"type": "string", "maxLength": 100},
        "window_id": window_id,
    }
    atspi_requirements = (
        "AT-SPI 2",
        "application accessibility support",
        "same-user accessibility bus",
    )
    register(
        ToolSpec(
            "accessibility.status",
            "ACCESSIBILITY",
            "Report the real AT-SPI session state and applications currently exposing semantic UI trees.",
            Permission.SAFE,
            EMPTY_SCHEMA,
            accessibility_status,
            platform_requirements=atspi_requirements,
            verification="AT-SPI desktop registry observation",
            side_effects=(),
            expected_latency_ms=80,
        )
    )
    register(
        ToolSpec(
            "accessibility.session_enable",
            "ACCESSIBILITY",
            "Enable or disable both session-only AT-SPI status flags and verify them.",
            Permission.LOW_RISK,
            object_schema({"enabled": {"type": "boolean"}}, ["enabled"]),
            accessibility_enable,
            platform_requirements=atspi_requirements,
            verification="Read back org.a11y.Status IsEnabled and ScreenReaderEnabled",
            side_effects=(
                "changes session-only accessibility status",
                "applications may expose accessibility data after restart",
            ),
            expected_latency_ms=150,
        )
    )
    register(
        ToolSpec(
            "accessibility.elements.list",
            "ACCESSIBILITY",
            "List bounded semantic UI elements by application, accessible name, and role.",
            Permission.SAFE,
            object_schema(
                {
                    "application": {"type": "string", "maxLength": 200},
                    "query": {"type": "string", "maxLength": 500},
                    "role": {"type": "string", "maxLength": 100},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 250},
                }
            ),
            accessibility_list,
            platform_requirements=atspi_requirements,
            verification="Return accessible object paths and supported actions",
            side_effects=(),
            expected_latency_ms=200,
        )
    )
    register(
        ToolSpec(
            "accessibility.element.activate",
            "ACCESSIBILITY",
            "Activate one exact, unambiguous AT-SPI control bound to an active KWin window and report the native action result.",
            Permission.SENSITIVE,
            object_schema(accessibility_locator, ["application", "name", "window_id"]),
            accessibility_activate,
            confirmation_reason="Activating an application control can change data or trigger an action inside that application.",
            platform_requirements=atspi_requirements,
            verification="Active KWin ID, PID/title scope, and AT-SPI do_action result",
            side_effects=("activates an application control",),
            expected_latency_ms=300,
        )
    )
    register(
        ToolSpec(
            "accessibility.element.set_text",
            "ACCESSIBILITY",
            "Set one exact editable AT-SPI field bound to an active KWin window without submitting it and verify the text when readable.",
            Permission.SENSITIVE,
            object_schema(
                {**accessibility_locator, "text": {"type": "string", "maxLength": 10000}},
                ["application", "name", "text", "window_id"],
            ),
            accessibility_set_text,
            confirmation_reason="This writes text into an application field; E.V. will not submit it automatically.",
            platform_requirements=atspi_requirements,
            verification="Active KWin ID, PID/title scope, and editable-text readback",
            side_effects=("replaces text in an application field",),
            expected_latency_ms=300,
        )
    )

    capture_id = {"type": "string", "pattern": "^[0-9a-f]{32}$", "maxLength": 32}
    register(
        ToolSpec(
            "vision.status",
            "VISION",
            "Report real local screen-capture, understanding, OCR, pointer, retention, and upload capabilities.",
            Permission.SAFE,
            EMPTY_SCHEMA,
            vision_status,
            platform_requirements=("Wayland", "KDE Spectacle or grim"),
            verification="Executable discovery and retention policy",
            side_effects=(),
            expected_latency_ms=20,
        )
    )
    register(
        ToolSpec(
            "vision.capture",
            "VISION",
            "Capture the whole workspace, one supported output, or one exact KWin window to a private expiring local PNG.",
            Permission.SENSITIVE,
            object_schema({"output": {"type": "string", "maxLength": 100}, "window_id": window_id}),
            vision_capture,
            confirmation_reason="A screenshot may contain private information. It stays local and expires after ten minutes.",
            platform_requirements=("Wayland", "KDE Spectacle or grim", "KWin window model"),
            verification="PNG dimensions and SHA-256 after capture",
            side_effects=(
                "writes one private temporary screenshot",
                "may briefly focus the exact requested window on KDE",
            ),
            expected_latency_ms=500,
        )
    )
    register(
        ToolSpec(
            "vision.ocr",
            "VISION",
            "Read visible text and bounded coordinates from one exact private E.V. capture using isolated local OCR.",
            Permission.SENSITIVE,
            object_schema(
                {
                    "capture_id": capture_id,
                    "minimum_score": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                },
                ["capture_id"],
            ),
            vision_ocr,
            platform_requirements=("RapidOCR", "ONNX Runtime", "existing E.V. capture"),
            verification="OCR worker returns structured text with confidence and boxes",
            side_effects=(
                "loads local OCR models on demand",
                "keeps the source capture until expiry or explicit deletion",
            ),
            expected_latency_ms=3500,
            timeout_seconds=40,
        )
    )
    register(
        ToolSpec(
            "vision.capture.delete",
            "VISION",
            "Remove one exact E.V. temporary screenshot before its automatic expiry.",
            Permission.LOW_RISK,
            object_schema({"capture_id": capture_id}, ["capture_id"]),
            vision_delete,
            verification="Capture path no longer exists",
            side_effects=("deletes one exact temporary capture",),
            expected_latency_ms=30,
        )
    )

    register(
        ToolSpec(
            "memory.search",
            "MEMORY",
            "Search or list only the user's explicit E.V. memories.",
            Permission.SAFE,
            object_schema(
                {
                    "query": {"type": "string", "maxLength": 256},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                }
            ),
            memory_search,
        )
    )
    register(
        ToolSpec(
            "memory.remember",
            "MEMORY",
            "Create durable memory only when the user explicitly asks E.V. to remember it.",
            Permission.SENSITIVE,
            object_schema(
                {
                    "content": {"type": "string", "minLength": 1, "maxLength": 8000},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 64},
                        "maxItems": 20,
                    },
                },
                ["content"],
            ),
            memory_remember,
            confirmation_reason="This stores the supplied statement in E.V.'s durable local memory.",
        )
    )
    register(
        ToolSpec(
            "memory.forget",
            "MEMORY",
            "Delete one explicit memory by its exact ID.",
            Permission.DESTRUCTIVE,
            object_schema({"id": {"type": "string", "minLength": 32, "maxLength": 32}}, ["id"]),
            memory_forget,
            confirmation_reason="This permanently removes the selected explicit memory.",
        )
    )
