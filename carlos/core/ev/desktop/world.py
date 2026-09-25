from __future__ import annotations

from ev.platform import executable as _platform_executable

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import re
import time
from dataclasses import dataclass
from typing import Any

from .kwin_bridge import KWinBridge
from .input import DesktopInput


class EntityResolutionError(ValueError):
    def __init__(self, message: str, candidates: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.candidates = candidates or []


@dataclass(slots=True)
class WorldSnapshot:
    captured_monotonic: float
    data: dict[str, Any]


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.casefold()))


class DesktopWorldModel:
    """Refreshable semantic view of the KDE session.

    KWin is queried on demand and cached only briefly; no screenshot polling is
    used.  This keeps entity identifiers fresh without burning idle CPU.
    """

    def __init__(
        self,
        bridge: KWinBridge,
        stale_after_seconds: float = 0.35,
        input_state_path: Path | None = None,
    ) -> None:
        self.bridge = bridge
        self.stale_after_seconds = stale_after_seconds
        self._snapshot: WorldSnapshot | None = None
        self._refresh_lock = asyncio.Lock()
        self._window_history: list[dict[str, Any]] = []
        self.input = DesktopInput(self, state_path=input_state_path)

    async def snapshot(self, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if (
            not force
            and self._snapshot
            and now - self._snapshot.captured_monotonic <= self.stale_after_seconds
        ):
            return self._snapshot.data
        async with self._refresh_lock:
            now = time.monotonic()
            if (
                not force
                and self._snapshot
                and now - self._snapshot.captured_monotonic <= self.stale_after_seconds
            ):
                return self._snapshot.data
            started = time.perf_counter()
            data = await self.bridge.request("snapshot", {}, timeout=4.0)
            await self._merge_kscreen(data)
            data["backend"] = self.bridge.status
            data["captured_at_monotonic"] = now
            data["refresh_duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
            self._snapshot = WorldSnapshot(now, data)
            return data

    def invalidate(self) -> None:
        self._snapshot = None

    def remember_window(self, window: dict[str, Any], action: str) -> dict[str, Any]:
        """Keep a bounded, in-memory restore point before a window mutation."""

        restore_point = {
            "action": action,
            "captured_monotonic": time.monotonic(),
            "window": deepcopy(window),
        }
        self._window_history.append(restore_point)
        self._window_history = self._window_history[-24:]
        return deepcopy(restore_point)

    def peek_window_restore(self) -> dict[str, Any] | None:
        return deepcopy(self._window_history[-1]) if self._window_history else None

    def consume_window_restore(self) -> dict[str, Any] | None:
        return deepcopy(self._window_history.pop()) if self._window_history else None

    async def _merge_kscreen(self, data: dict[str, Any]) -> None:
        try:
            process = await asyncio.create_subprocess_exec(
                _platform_executable("/usr/bin/kscreen-doctor"),
                "-j",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=3)
            document = json.loads(stdout)
        except (OSError, TimeoutError, json.JSONDecodeError):
            return
        details = {str(item.get("name", "")): item for item in document.get("outputs", [])}
        for output in data.get("outputs", []):
            source = details.get(str(output.get("name", "")), {})
            current_id = str(source.get("currentModeId", ""))
            current_mode = next(
                (mode for mode in source.get("modes", []) if str(mode.get("id")) == current_id), {}
            )
            output.update(
                {
                    "id": source.get("id"),
                    "connected": bool(source.get("connected", True)),
                    "enabled": bool(source.get("enabled", True)),
                    "priority": int(source.get("priority", 0)),
                    "position": source.get(
                        "pos",
                        {
                            "x": output.get("geometry", {}).get("x", 0),
                            "y": output.get("geometry", {}).get("y", 0),
                        },
                    ),
                    "refresh_rate": round(float(current_mode.get("refreshRate", 0.0)), 3),
                    "resolution": current_mode.get(
                        "size",
                        {
                            "width": output.get("geometry", {}).get("width", 0),
                            "height": output.get("geometry", {}).get("height", 0),
                        },
                    ),
                    "primary": int(source.get("priority", 0)) == 1,
                }
            )

    @staticmethod
    def visible_windows(world: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            window
            for window in world.get("windows", [])
            if not window.get("special")
            and (window.get("normal") or window.get("dialog"))
            and int(window.get("geometry", {}).get("width", 0)) > 16
            and int(window.get("geometry", {}).get("height", 0)) > 16
            and any(
                str(window.get(key, "")).strip() for key in ("title", "app_id", "resource_class")
            )
            and str(window.get("resource_class", "")).casefold() != "kwin_wayland"
        ]

    def resolve_window(
        self,
        description: str,
        world: dict[str, Any],
        *,
        exclude_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        query = description.strip().casefold()
        excluded = exclude_ids or set()
        windows = [
            window
            for window in self.visible_windows(world)
            if str(window.get("id")) not in excluded
        ]
        if not windows:
            raise EntityResolutionError("No controllable windows are currently visible")
        if query.startswith("window-id:"):
            window_id = description.strip()[len("window-id:") :]
            match = next((window for window in windows if str(window.get("id")) == window_id), None)
            if match:
                return match
            raise EntityResolutionError("The previously selected window is no longer available")
        if query in {"this", "that", "it", "current", "active", "this window", "current window"}:
            active_id = str(world.get("active_window_id", ""))
            match = next((window for window in windows if str(window.get("id")) == active_id), None)
            if match:
                return match
            raise EntityResolutionError("KWin did not report an active controllable window")

        aliases = {
            "browser": {"firefox", "chromium", "chrome", "browser"},
            "firefox": {"firefox", "org", "mozilla"},
            "terminal": {"terminal", "konsole", "kitty", "alacritty", "xterm", "ptyxis"},
            "console": {"terminal", "konsole", "kitty", "console"},
            "code": {"code", "visual", "studio", "vscode"},
            "vs code": {"code", "visual", "studio", "vscode"},
        }
        base_query_tokens = _tokens(query) - {
            "a",
            "an",
            "the",
            "my",
            "please",
            "window",
            "app",
            "application",
            "is",
            "on",
        }
        query_tokens = set(base_query_tokens)
        alias_used = False
        for alias, expansion in aliases.items():
            if alias in query:
                query_tokens |= expansion
                alias_used = True
        scored: list[tuple[int, dict[str, Any]]] = []
        active_id = str(world.get("active_window_id", ""))
        for window in windows:
            title = str(window.get("title", ""))
            app_id = str(window.get("app_id", ""))
            identity = " ".join(
                (
                    title,
                    app_id,
                    str(window.get("resource_class", "")),
                    str(window.get("resource_name", "")),
                )
            )
            identity_lower = identity.casefold()
            identity_tokens = _tokens(identity)
            base_matches = len(base_query_tokens & identity_tokens)
            exact_phrase = bool(query and query in identity_lower)
            # A single coincidental token from a detailed description is not
            # enough to target a different window (for example "EV" alone must
            # not resolve "EV Phase 3 Disposable Terminal" to the E.V. UI).
            if (
                len(base_query_tokens) >= 2
                and not exact_phrase
                and base_matches / len(base_query_tokens) < 0.5
            ):
                continue
            score = len(query_tokens & identity_tokens) * 20
            if exact_phrase:
                score += 70
            if app_id and app_id.casefold() in query:
                score += 60
            if str(window.get("id")) == active_id:
                score += 3
            if score > 0 and (base_matches > 0 or alias_used):
                scored.append((score, window))
        scored.sort(key=lambda pair: (pair[0], int(pair[1].get("stacking_order", 0))), reverse=True)
        if not scored:
            raise EntityResolutionError(f"No window matches {description!r}")
        top_score = scored[0][0]
        top = [window for score, window in scored if score == top_score]
        if len(top) > 1:
            active = next((window for window in top if str(window.get("id")) == active_id), None)
            if active:
                return active
            raise EntityResolutionError(
                f"Multiple windows match {description!r}",
                [
                    {"id": item.get("id"), "title": item.get("title"), "app_id": item.get("app_id")}
                    for item in top[:6]
                ],
            )
        return scored[0][1]

    def resolve_output(self, description: str, world: dict[str, Any]) -> dict[str, Any]:
        outputs = [item for item in world.get("outputs", []) if item.get("enabled", True)]
        if not outputs:
            raise EntityResolutionError("KWin did not report an enabled output")
        query = description.strip().casefold()
        active_name = str(world.get("active_output", ""))
        if query in {"this", "current", "this monitor", "current monitor", "monitor i'm using"}:
            return next((item for item in outputs if item.get("name") == active_name), outputs[0])
        if "other" in query:
            others = [item for item in outputs if item.get("name") != active_name]
            if len(others) == 1:
                return others[0]
            if not others:
                raise EntityResolutionError("There is no other enabled monitor")
        if "laptop" in query or "built-in" in query or "builtin" in query:
            matches = [
                item
                for item in outputs
                if str(item.get("name", "")).casefold().startswith(("edp", "lvds"))
            ]
        elif "external" in query:
            matches = [
                item
                for item in outputs
                if not str(item.get("name", "")).casefold().startswith(("edp", "lvds"))
            ]
        elif "left" in query:
            return min(outputs, key=lambda item: int(item.get("geometry", {}).get("x", 0)))
        elif "right" in query:
            return max(outputs, key=lambda item: int(item.get("geometry", {}).get("x", 0)))
        elif "main" in query or "primary" in query:
            matches = [item for item in outputs if item.get("primary")]
        elif "second" in query or re.search(r"\b2\b", query):
            ordered = sorted(
                outputs,
                key=lambda item: (
                    int(item.get("priority", 999) or 999),
                    int(item.get("id", 999) or 999),
                ),
            )
            if len(ordered) < 2:
                raise EntityResolutionError("Only one monitor is enabled")
            return ordered[1]
        else:
            matches = [item for item in outputs if query in str(item.get("name", "")).casefold()]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise EntityResolutionError(f"No monitor matches {description!r}")
        raise EntityResolutionError(f"Multiple monitors match {description!r}", matches)

    def resolve_desktop(self, description: str, world: dict[str, Any]) -> dict[str, Any]:
        desktops = list(world.get("desktops", []))
        if not desktops:
            raise EntityResolutionError("KWin did not report any virtual desktops")
        query = description.strip().casefold()
        if query in {
            "this",
            "current",
            "this workspace",
            "current workspace",
            "this desktop",
            "current desktop",
        }:
            current = str(world.get("current_desktop", ""))
            match = next((item for item in desktops if str(item.get("id")) == current), None)
            if match:
                return match
        number_match = re.search(r"\b(\d+)\b", query)
        if number_match:
            index = int(number_match.group(1)) - 1
            if 0 <= index < len(desktops):
                return desktops[index]
            raise EntityResolutionError(f"Virtual desktop {index + 1} does not exist")
        matches = [
            item for item in desktops if query and query in str(item.get("name", "")).casefold()
        ]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise EntityResolutionError(f"No virtual desktop matches {description!r}")
        raise EntityResolutionError(f"Multiple virtual desktops match {description!r}", matches)
