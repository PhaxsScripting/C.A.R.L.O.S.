"""On-demand, scoped desktop evidence; never an idle screen recorder."""

from __future__ import annotations

import asyncio
import time
from typing import Any


class DesktopObservation:
    def __init__(self, desktop: Any, accessibility: Any) -> None:
        self.desktop = desktop
        self.accessibility = accessibility

    async def observe(
        self, *, window_id: str = "", level: str = "basic", limit: int = 100, fresh: bool = True
    ) -> dict[str, Any]:
        if level not in {"basic", "accessibility"} or not 1 <= limit <= 150:
            raise ValueError("Unsupported observation level or element limit")
        started = time.monotonic()
        world = await self.desktop.snapshot(force=fresh)
        windows = self.desktop.visible_windows(world)
        target_id = window_id or str(world.get("active_window_id", ""))
        target = next((w for w in windows if str(w.get("id")) == target_id), None)
        input_status = self.desktop.input.status()
        observation: dict[str, Any] = {
            "ok": True,
            "scope": "desktop_observation",
            "level": level,
            "captured_at_monotonic": world.get("captured_at_monotonic", started),
            "age_ms": round(
                max(0, time.monotonic() - float(world.get("captured_at_monotonic", started)))
                * 1000,
                3,
            ),
            "active_window_id": world.get("active_window_id", ""),
            "target_window": target,
            "windows": windows[:100],
            "windows_truncated": len(windows) > 100,
            "outputs": world.get("outputs", []),
            "cursor": world.get("cursor", {}),
            "input": input_status,
            "warnings": [],
            "accessibility": {"status": "NOT_REQUESTED"},
            "capture_performed": False,
        }
        if not input_status.get("connected"):
            observation["warnings"].append(
                {
                    "code": "INPUT_DISCONNECTED",
                    "message": input_status.get("reason", "Native desktop input is disconnected"),
                }
            )
        if window_id and target is None:
            return {
                **observation,
                "ok": False,
                "error": "The exact requested window is no longer available",
            }
        if level == "accessibility":
            if not target or not target.get("pid") or not str(target.get("title", "")).strip():
                observation["accessibility"] = {
                    "status": "UNAVAILABLE",
                    "reason": "No exact process and window title for semantic inspection",
                }
            else:
                try:
                    listing = await asyncio.wait_for(
                        asyncio.to_thread(
                            self.accessibility.list_elements,
                            "",
                            "",
                            "",
                            limit,
                            int(target["pid"]),
                            str(target["title"]),
                        ),
                        timeout=4,
                    )
                    # Accessibility and KWin are not atomic. Bind again before
                    # returning controls the model might act on later.
                    after = await self.desktop.snapshot(force=True)
                    current = next(
                        (w for w in after.get("windows", []) if str(w.get("id")) == target_id), None
                    )
                    identity_changed = not current or any(
                        current.get(key) != target.get(key) for key in ("pid", "title")
                    )
                    focus_changed = not window_id and after.get("active_window_id") != target_id
                    if identity_changed or focus_changed:
                        observation["accessibility"] = {
                            "status": "STALE",
                            "reason": "Target identity or active focus changed during inspection; observe again",
                        }
                    else:
                        elements = listing.get("elements", [])
                        observation["accessibility"] = {
                            "status": (
                                "PARTIAL"
                                if listing.get("truncated")
                                else "OBSERVED" if elements else "EMPTY"
                            ),
                            "window_id": target_id,
                            "process_id": target["pid"],
                            "window_title": target["title"],
                            "elements": elements[:limit],
                            "truncated": bool(listing.get("truncated")) or len(elements) > limit,
                        }
                except (TimeoutError, RuntimeError, OSError, ValueError) as error:
                    observation["accessibility"] = {"status": "UNAVAILABLE", "reason": str(error)}
        observation["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
        return observation
