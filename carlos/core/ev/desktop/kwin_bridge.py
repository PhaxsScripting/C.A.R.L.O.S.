from __future__ import annotations

from ev.platform import executable as _platform_executable

import asyncio
import json
import logging
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any


class KWinBridge:
    """Narrow JSON command bridge between E.V. and KWin's scripting API.

    KWin remains the authority for Wayland window state.  The bridge exposes no
    network listener and accepts commands only from the same session D-Bus.
    """

    INTERFACE = "com.ev.KWinBridge"
    OBJECT_PATH = "/com/ev/KWinBridge"
    PLUGIN_ID = "org.phax.ev.bridge.runtime"

    def __init__(self, script_path: Path, logger: logging.Logger) -> None:
        self.script_path = script_path
        self.logger = logger
        self._condition = threading.Condition()
        self._commands: deque[dict[str, Any]] = deque()
        self._results: dict[str, dict[str, Any]] = {}
        self._pending: set[str] = set()
        self._script_loaded = False
        self._start_lock = asyncio.Lock()
        self._closed = False
        self._bus: Any = None
        self._object: Any = None
        self._glib_loop: Any = None
        self._glib_thread: threading.Thread | None = None
        self._available = False
        self._reason = "KWin bridge has not started"
        self._kwin_bus_owner = ""

    @property
    def status(self) -> dict[str, Any]:
        return {
            "available": self._available,
            "reason": self._reason,
            "backend": "kwin-script-dbus",
            "script": str(self.script_path),
            "network_exposed": False,
        }

    def attach_session_bus(self, bus: Any) -> None:
        import dbus.service
        from gi.repository import GLib

        owner = self
        try:
            self._kwin_bus_owner = str(bus.get_name_owner("org.kde.KWin"))
        except Exception:
            self._kwin_bus_owner = ""

        class BridgeObject(dbus.service.Object):
            @dbus.service.method(
                KWinBridge.INTERFACE, in_signature="", out_signature="s", sender_keyword="sender"
            )
            def NextCommand(self, sender: str | None = None) -> str:  # noqa: N802 - D-Bus API
                if str(sender or "") != owner._kwin_bus_owner:
                    owner.logger.warning("Rejected KWin bridge poll from unexpected D-Bus peer")
                    return ""
                return owner._next_command()

            @dbus.service.method(
                KWinBridge.INTERFACE, in_signature="s", out_signature="b", sender_keyword="sender"
            )
            def Report(self, raw: str, sender: str | None = None) -> bool:  # noqa: N802 - D-Bus API
                if str(sender or "") != owner._kwin_bus_owner:
                    owner.logger.warning("Rejected KWin bridge report from unexpected D-Bus peer")
                    return False
                return owner._report(str(raw))

        self._bus = bus
        self._object = BridgeObject(bus, self.OBJECT_PATH)
        self._glib_loop = GLib.MainLoop()
        self._glib_thread = threading.Thread(
            target=self._glib_loop.run, name="ev-dbus-bridge", daemon=True
        )
        self._glib_thread.start()

    async def start(self) -> None:
        async with self._start_lock:
            await self._start()

    async def _start(self) -> None:
        self._available = False
        if self._closed:
            self._reason = "KWin bridge is closed"
            return
        if self._bus is None:
            self._reason = "E.V. does not own its session D-Bus name"
            return
        if not self.script_path.is_file():
            self._reason = f"KWin bridge script is missing: {self.script_path}"
            return
        try:
            self._kwin_bus_owner = str(self._bus.get_name_owner("org.kde.KWin"))
        except Exception:
            self._reason = "KWin is not available on the session bus"
            return
        await self._qdbus("unloadScript", self.PLUGIN_ID, allow_failure=True)
        loaded = await self._qdbus("loadScript", str(self.script_path), self.PLUGIN_ID)
        try:
            script_id = int(loaded.strip())
        except ValueError:
            script_id = -1
        if script_id < 0:
            self._reason = f"KWin rejected the E.V. bridge script: {loaded.strip()}"
            return
        self._script_loaded = True
        await self._qdbus("start")
        try:
            result = await self.request("ping", {}, timeout=3.0)
        except Exception as error:
            self._reason = f"KWin bridge did not answer: {error}"
            return
        self._available = bool(result.get("pong"))
        self._reason = (
            "KWin Wayland bridge is active"
            if self._available
            else str(result.get("error", "KWin bridge failed"))
        )

    async def close(self) -> None:
        self._available = False
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        # An isolated CoreService/test that never owned a bridge must not
        # unload the live desktop core's script during teardown.
        if self._script_loaded:
            await self._qdbus("unloadScript", self.PLUGIN_ID, allow_failure=True)
            self._script_loaded = False
        if self._object is not None:
            try:
                self._object.remove_from_connection()
            except Exception:
                pass
            self._object = None
        if self._glib_loop is not None:
            self._glib_loop.quit()
        if self._glib_thread is not None:
            self._glib_thread.join(timeout=1.0)

    async def _qdbus(self, method: str, *arguments: str, allow_failure: bool = False) -> str:
        process = await asyncio.create_subprocess_exec(
            _platform_executable("/usr/bin/qdbus6"),
            "org.kde.KWin",
            "/Scripting",
            f"org.kde.kwin.Scripting.{method}",
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        if process.returncode and not allow_failure:
            raise RuntimeError(
                stderr.decode(errors="replace").strip() or f"qdbus exited {process.returncode}"
            )
        return stdout.decode(errors="replace")

    def _next_command(self) -> str:
        deadline = time.monotonic() + 0.75
        with self._condition:
            while not self._commands and not self._closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return ""
                self._condition.wait(remaining)
            if self._closed or not self._commands:
                return ""
            return json.dumps(self._commands.popleft(), separators=(",", ":"))

    def _report(self, raw: str) -> bool:
        try:
            result = json.loads(raw)
            request_id = str(result["id"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return False
        with self._condition:
            if request_id not in self._pending:
                return False
            self._results[request_id] = result
            self._condition.notify_all()
        return True

    def _wait_for_result(self, request_id: str, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        with self._condition:
            while request_id not in self._results and not self._closed:
                if request_id not in self._pending:
                    raise RuntimeError("KWin request was cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"KWin did not finish {request_id} within {timeout:.1f}s")
                self._condition.wait(remaining)
            if self._closed:
                raise RuntimeError("KWin bridge stopped")
            return self._results.pop(request_id)

    async def request(
        self, action: str, arguments: dict[str, Any], timeout: float = 4.0
    ) -> dict[str, Any]:
        if action != "ping" and self._bus is not None and not self._available:
            await self.start()
            if not self._available:
                raise RuntimeError(self._reason)
        try:
            return await self._request_once(action, arguments, timeout)
        except TimeoutError:
            self._available = False
            self._reason = "KWin bridge stopped answering"
            # Only replay a read. A timed-out mutation may already have run.
            if action == "snapshot" and self._bus is not None and not self._closed:
                await self.start()
                if self._available:
                    return await self._request_once(action, arguments, timeout)
            raise

    async def _request_once(
        self, action: str, arguments: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("KWin bridge is closed")
        request_id = uuid.uuid4().hex
        command = {
            "id": request_id,
            "action": action,
            "arguments": arguments,
            "deadline_unix_ms": int((time.time() + timeout) * 1000),
        }
        with self._condition:
            self._pending.add(request_id)
            self._commands.append(command)
            self._condition.notify_all()
        try:
            reply = await asyncio.wait_for(
                asyncio.to_thread(self._wait_for_result, request_id, timeout),
                timeout=timeout + 0.5,
            )
        finally:
            with self._condition:
                self._pending.discard(request_id)
                self._results.pop(request_id, None)
                self._commands = deque(item for item in self._commands if item["id"] != request_id)
                self._condition.notify_all()
        if not bool(reply.get("ok")):
            raise RuntimeError(str(reply.get("error", f"KWin action {action} failed")))
        result = reply.get("result", {})
        if not isinstance(result, dict):
            raise RuntimeError("KWin bridge returned a malformed result")
        return result

    @staticmethod
    def check_runtime_dependencies() -> tuple[bool, str]:
        required = (_platform_executable("/usr/bin/qdbus6"),)
        missing = [path for path in required if not Path(path).is_file()]
        if missing:
            return False, "Missing: " + ", ".join(missing)
        try:
            subprocess.run(
                [
                    _platform_executable("/usr/bin/qdbus6"),
                    "org.kde.KWin",
                    "/KWin",
                    "org.freedesktop.DBus.Peer.Ping",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=True,
            )
        except (OSError, subprocess.SubprocessError):
            return False, "KWin is not reachable on the session D-Bus"
        return True, "KWin and qdbus6 are available"
