from __future__ import annotations

from ev.platform import executable as _platform_executable

import asyncio
import hashlib
import os
import json
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any


class ScreenPerception:
    """On-demand, local-only Wayland capture with bounded private retention."""

    def __init__(
        self, capture_root: Path, desktop: Any, config: dict[str, Any] | None = None
    ) -> None:
        self.capture_root = capture_root
        self.desktop = desktop
        self.config = config or {}
        from .ai.local_vision import LocalVisualReasoner

        self.reasoner = LocalVisualReasoner(
            self.config.get("local_model", {}), capture_root / "vision-model-owner.json"
        )
        self.capture_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.capture_root, 0o700)
        self.prune()

    @staticmethod
    def _backend() -> tuple[str, str]:
        spectacle = Path(_platform_executable("/usr/bin/spectacle"))
        if spectacle.is_file():
            return "spectacle", str(spectacle)
        grim = Path(_platform_executable("/usr/bin/grim"))
        if grim.is_file():
            return "grim", str(grim)
        return "", ""

    def status(self) -> dict[str, Any]:
        backend, executable = self._backend()
        input_controller = getattr(self.desktop, "input", None)
        input_status = input_controller.status() if input_controller is not None else {}
        ocr_python = Path(str(self.config.get("ocr_python", ""))).expanduser()
        ocr_available = ocr_python.is_file() and any(
            path.is_dir()
            for path in (ocr_python.parent.parent / "lib").glob("python*/site-packages/rapidocr")
        )
        return {
            "available": bool(backend),
            "capture_backend": backend or "unavailable",
            "capture_executable": executable,
            "workspace_capture": bool(backend),
            "exact_window_capture": bool(backend),
            "named_output_capture": backend == "grim",
            # Text recognition is not scene understanding or a vision model.
            "semantic_understanding": False,
            "visual_reasoning": (
                "available_unverified" if self.reasoner.status()["available"] else "not_configured"
            ),
            "local_model": self.reasoner.status(),
            "ocr": ocr_available,
            "ocr_engine": "RapidOCR 3.9.2 / ONNX Runtime" if ocr_available else "unavailable",
            "ocr_python": str(ocr_python) if ocr_available else "",
            "pointer_control": bool(input_status.get("pointer")),
            "pointer_backend_available": bool(input_status.get("available")),
            "input_session": input_status,
            "continuous_capture": False,
            "network_upload": False,
            "retention_seconds": 600,
            "reason": (
                f"Private on-demand capture is ready through {backend}"
                + (" with local OCR" if ocr_available else "")
                if backend
                else "Neither KDE Spectacle nor grim is installed"
            ),
        }

    def _capture_path(self, capture_id: str) -> Path:
        self.prune()
        if len(capture_id) != 32 or any(
            character not in "0123456789abcdef" for character in capture_id
        ):
            raise ValueError("invalid capture id")
        path = self.capture_root / f"{capture_id}.png"
        if not path.is_file() or path.is_symlink():
            raise ValueError("capture does not exist or has expired")
        return path

    async def describe(self, capture_id: str, question: str) -> dict[str, Any]:
        result = await self.reasoner.describe(self._capture_path(capture_id), question)
        return {**result, "capture_id": capture_id}

    async def inspect_window(self, window_id: str, question: str) -> dict[str, Any]:
        """Bind slow visual reasoning to one window; never act on its guess."""
        if not window_id:
            raise ValueError("An exact window ID is required")
        if not self.reasoner.status()["available"]:
            raise RuntimeError(self.reasoner.status()["reason"])
        world = await self.desktop.snapshot(force=True)
        before = next((w for w in world.get("windows", []) if w.get("id") == window_id), None)
        if not before or not before.get("pid"):
            raise ValueError("Exact window is unavailable")
        # Copy nested geometry; backend caches can mutate during inference.
        from copy import deepcopy

        before = deepcopy(before)
        capture = await self.capture(window_id=window_id)
        try:
            result = await self.describe(capture["capture_id"], question)
            after_world = await self.desktop.snapshot(force=True)
            after = next(
                (w for w in after_world.get("windows", []) if w.get("id") == window_id), None
            )
            unchanged = after is not None and all(
                before.get(k) == after.get(k) for k in ("pid", "title", "app_id", "geometry")
            )
            return {
                **result,
                "target": {k: before.get(k) for k in ("id", "pid", "title", "app_id", "geometry")},
                "target_identity_unchanged": unchanged,
                "stale": not unchanged,
                "focus_restored": capture.get("focus_restored"),
                "capture_retained": False,
                "scope": "Historical image inference; unchanged window identity does not prove unchanged contents",
                "warnings": (
                    []
                    if unchanged
                    else [
                        "Target identity or geometry changed while reasoning; observe again before any action"
                    ]
                ),
            }
        finally:
            self.delete(capture["capture_id"])

    def ocr(self, capture_id: str, minimum_score: float = 0.55) -> dict[str, Any]:
        path = self._capture_path(capture_id)
        status = self.status()
        if not status["ocr"]:
            raise RuntimeError("The local OCR runtime is unavailable")
        command = [
            _platform_executable("/usr/bin/nice"),
            "-n",
            str(int(self.config.get("ocr_nice", 15))),
            str(self.config["ocr_python"]),
            "-m",
            "ev.ocr_worker",
            "--image",
            str(path),
            "--minimum-score",
            str(minimum_score),
        ]
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=float(self.config.get("ocr_timeout_seconds", 30)),
            check=False,
        )
        marker = "EV_OCR_JSON:"
        payload_line = next(
            (line for line in reversed(result.stdout.splitlines()) if line.startswith(marker)), ""
        )
        if result.returncode != 0 or not payload_line:
            detail = (
                result.stderr.strip().splitlines()[-1]
                if result.stderr.strip()
                else "local OCR failed"
            )
            raise RuntimeError(detail[:500])
        try:
            payload = json.loads(payload_line[len(marker) :])
        except json.JSONDecodeError as error:
            raise RuntimeError("local OCR returned invalid data") from error
        return {
            "verified": True,
            "capture_id": capture_id,
            "engine": payload.get("engine", "RapidOCR"),
            "elements": payload.get("elements", [])[:250],
            "text": str(payload.get("text", ""))[:65536],
            "count": min(int(payload.get("count", 0)), 250),
            "duration_ms": float(payload.get("duration_ms", 0.0)),
            "local_only": True,
            "uploaded": False,
            "capture_retained": True,
        }

    async def capture(self, output: str = "", window_id: str = "") -> dict[str, Any]:
        if output and window_id:
            raise ValueError("choose either output or window_id, not both")
        if not self.status()["available"]:
            raise RuntimeError(self.status()["reason"])
        self.prune()
        backend, executable = self._backend()
        expected: dict[str, Any] | None = None
        kind = "workspace"
        original_active_id = ""
        focus_restored = True
        if window_id:
            world = await self.desktop.snapshot(force=True)
            window = next(
                (item for item in world["windows"] if str(item.get("id")) == window_id), None
            )
            if window is None:
                raise RuntimeError("window no longer exists")
            expected = dict(window["geometry"])
            original_active_id = str(world.get("active_window_id", ""))
            kind = "window"
        elif output:
            world = await self.desktop.snapshot(force=True)
            match = next(
                (
                    item
                    for item in world["outputs"]
                    if str(item.get("name")) == output and item.get("enabled", True)
                ),
                None,
            )
            if match is None:
                raise RuntimeError("output is unavailable")
            expected = dict(match["geometry"])
            kind = "output"
        capture_id = uuid.uuid4().hex
        path = self.capture_root / f"{capture_id}.png"
        try:
            if backend == "spectacle":
                if output:
                    raise RuntimeError(
                        "named-output capture is unavailable through KDE Spectacle; capture the workspace or an exact window"
                    )
                if window_id:
                    await self.desktop.bridge.request("activate", {"window_id": window_id})
                    # KWin activation is asynchronous. Observe a bounded
                    # settle window instead of assuming a fixed 120ms delay.
                    deadline = time.monotonic() + 1.0
                    while True:
                        await asyncio.sleep(0.05)
                        active_world = await self.desktop.snapshot(force=True)
                        if (
                            str(active_world.get("active_window_id", "")) == window_id
                            or time.monotonic() >= deadline
                        ):
                            break
                    if str(active_world.get("active_window_id", "")) != window_id:
                        raise RuntimeError("KWin did not activate the exact window for capture")
                    active = next(
                        (
                            item
                            for item in active_world["windows"]
                            if str(item.get("id")) == window_id
                        ),
                        None,
                    )
                    if active is None:
                        raise RuntimeError("window disappeared before capture")
                    expected = dict(active["geometry"])
                    arguments = [
                        executable,
                        "--background",
                        "--nonotify",
                        "--activewindow",
                        "--no-shadow",
                        "--output",
                        str(path),
                    ]
                else:
                    arguments = [
                        executable,
                        "--background",
                        "--nonotify",
                        "--fullscreen",
                        "--output",
                        str(path),
                    ]
            else:
                arguments = [executable, "-t", "png"]
                if expected is not None:
                    if output:
                        arguments.extend(["-o", output])
                    else:
                        arguments.extend(
                            [
                                "-g",
                                f"{expected['x']},{expected['y']} {expected['width']}x{expected['height']}",
                            ]
                        )
                arguments.append(str(path))
            spawning = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    *arguments,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            )
            try:
                process = await asyncio.shield(spawning)
            except asyncio.CancelledError:
                # Acquire the exact child even if cancellation arrived during
                # spawn, so no screenshot process escapes cleanup.
                process = await spawning
                await self._stop_capture_process(process)
                path.unlink(missing_ok=True)
                raise
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
            except (TimeoutError, asyncio.CancelledError) as error:
                await self._stop_capture_process(process)
                path.unlink(missing_ok=True)
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise RuntimeError("screen capture timed out") from error
            if backend == "spectacle" and window_id and process.returncode == 0:
                after_capture = await self.desktop.snapshot(force=True)
                still_target = next(
                    (w for w in after_capture.get("windows", []) if w.get("id") == window_id), None
                )
                if (
                    after_capture.get("active_window_id") != window_id
                    or still_target is None
                    or any(
                        still_target.get(k) != active.get(k) for k in ("pid", "title", "geometry")
                    )
                ):
                    path.unlink(missing_ok=True)
                    raise RuntimeError(
                        "Window changed during capture; discarded uncertain screenshot"
                    )
        finally:
            if (
                backend == "spectacle"
                and window_id
                and original_active_id
                and original_active_id != window_id
            ):
                try:
                    await self.desktop.bridge.request("activate", {"window_id": original_active_id})
                    await asyncio.sleep(0.08)
                    restored_world = await self.desktop.snapshot(force=True)
                    focus_restored = (
                        str(restored_world.get("active_window_id", "")) == original_active_id
                    )
                except Exception:
                    focus_restored = False
        if process.returncode != 0 or not path.is_file():
            path.unlink(missing_ok=True)
            raise RuntimeError(
                stderr.decode(errors="replace").strip()
                or stdout.decode(errors="replace").strip()
                or "screen capture failed"
            )
        os.chmod(path, 0o600)
        from PIL import Image

        try:
            with Image.open(path) as image:
                width, height = image.size
                mode = image.mode
        except Exception as error:
            path.unlink(missing_ok=True)
            raise RuntimeError("capture backend returned an invalid PNG") from error
        if expected is not None and (
            width != int(expected["width"]) or height != int(expected["height"])
        ):
            path.unlink(missing_ok=True)
            raise RuntimeError("captured dimensions do not match the requested target")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return {
            "captured": True,
            "verified": True,
            "capture_id": capture_id,
            "kind": kind,
            "path": str(path),
            "width": width,
            "height": height,
            "mode": mode,
            "bytes": path.stat().st_size,
            "sha256": digest,
            "expires_in_seconds": 600,
            "local_only": True,
            "uploaded": False,
            "semantic_understanding": False,
            "capture_backend": backend,
            "focus_restored": focus_restored,
        }

    @staticmethod
    async def _stop_capture_process(process) -> None:
        if process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=1)
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()

    def delete(self, capture_id: str) -> dict[str, Any]:
        path = self.capture_root / f"{capture_id}.png"
        if len(capture_id) != 32 or any(
            character not in "0123456789abcdef" for character in capture_id
        ):
            raise ValueError("invalid capture id")
        existed = path.is_file()
        if existed:
            path.unlink()
        return {"verified": not path.exists(), "capture_id": capture_id, "removed": existed}

    def prune(self) -> int:
        cutoff = time.time() - 600
        removed = 0
        for path in self.capture_root.glob("[0-9a-f]" * 32 + ".png"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed
