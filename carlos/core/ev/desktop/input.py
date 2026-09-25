from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
from queue import Empty
import tempfile
import threading
import uuid
import math
import time
from typing import Any


class RemoteDesktopPortal:
    """Session-scoped input through the compositor's existing consent portal.

    Jeepney's private receiver thread dispatches portal response signals. No
    root helper, background screen stream, or listening socket is created.
    """

    SERVICE = "org.freedesktop.portal.Desktop"
    PATH = "/org/freedesktop/portal/desktop"
    INTERFACE = "org.freedesktop.portal.RemoteDesktop"

    def __init__(self, state_path: Path | None = None) -> None:
        self.state_path = state_path
        self._router_context: Any = None
        self._router: Any = None
        self._bus_proxy: Any = None
        self._remote: Any = None
        self._properties: Any = None
        self._session = ""
        self._devices = 0
        self._closed_rule: Any = None
        self._closed_filter: Any = None
        self._closed_queue: Any = None
        self._cancelled = threading.Event()

    def status(self) -> dict[str, Any]:
        self._poll_session_closed()
        supported = (
            bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS"))
            and importlib.util.find_spec("jeepney") is not None
        )
        return {
            "available": supported,
            "connected": bool(self._session and self._devices),
            "keyboard": bool(self._devices & 1),
            "pointer": bool(self._devices & 2),
            "backend": "xdg-desktop-portal",
            "session_consent_required": not bool(self._devices),
            "network_exposed": False,
            "permission_restore_enabled": self.state_path is not None,
            "reason": (
                "KDE input session is active"
                if self._devices
                else (
                    "Start a desktop input session; KDE may show its native permission dialog"
                    if supported
                    else "The session D-Bus client or desktop session bus is unavailable"
                )
            ),
        }

    @staticmethod
    def _unvariant(value: Any) -> Any:
        if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], str):
            return RemoteDesktopPortal._unvariant(value[1])
        if isinstance(value, dict):
            return {str(key): RemoteDesktopPortal._unvariant(item) for key, item in value.items()}
        if isinstance(value, list):
            return [RemoteDesktopPortal._unvariant(item) for item in value]
        return value

    def _ensure_connection(self) -> None:
        if self._router is not None:
            return
        from jeepney.bus_messages import message_bus
        from jeepney.io.threading import Proxy, open_dbus_router
        from jeepney.wrappers import MessageGenerator, Properties, new_method_call

        class RemoteDesktopMessages(MessageGenerator):
            interface = RemoteDesktopPortal.INTERFACE

            def CreateSession(self, options: dict[str, Any]) -> Any:
                return new_method_call(self, "CreateSession", "a{sv}", (options,))

            def SelectDevices(self, session: str, options: dict[str, Any]) -> Any:
                return new_method_call(self, "SelectDevices", "oa{sv}", (session, options))

            def Start(self, session: str, parent: str, options: dict[str, Any]) -> Any:
                return new_method_call(self, "Start", "osa{sv}", (session, parent, options))

            def NotifyPointerMotion(
                self, session: str, options: dict[str, Any], dx: float, dy: float
            ) -> Any:
                return new_method_call(
                    self, "NotifyPointerMotion", "oa{sv}dd", (session, options, dx, dy)
                )

            def NotifyPointerButton(
                self, session: str, options: dict[str, Any], button: int, state: int
            ) -> Any:
                return new_method_call(
                    self, "NotifyPointerButton", "oa{sv}iu", (session, options, button, state)
                )

            def NotifyPointerAxisDiscrete(
                self, session: str, options: dict[str, Any], axis: int, steps: int
            ) -> Any:
                return new_method_call(
                    self, "NotifyPointerAxisDiscrete", "oa{sv}ui", (session, options, axis, steps)
                )

            def NotifyKeyboardKeysym(
                self, session: str, options: dict[str, Any], keysym: int, state: int
            ) -> Any:
                return new_method_call(
                    self, "NotifyKeyboardKeysym", "oa{sv}iu", (session, options, keysym, state)
                )

        context = open_dbus_router(bus="SESSION")
        try:
            router = context.__enter__()
            messages = RemoteDesktopMessages(self.PATH, self.SERVICE)
            self._router_context = context
            self._router = router
            self._bus_proxy = Proxy(message_bus, router, timeout=5)
            self._remote = Proxy(messages, router, timeout=5)
            self._properties = Proxy(Properties(messages), router, timeout=5)
        except BaseException:
            try:
                context.__exit__(None, None, None)
            except Exception:
                pass
            self._router_context = None
            self._router = None
            self._bus_proxy = None
            self._remote = None
            self._properties = None
            raise

    def _request(
        self, method: str, *args: Any, options: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        from jeepney.bus_messages import MatchRule

        token = "ev_" + uuid.uuid4().hex
        sender = str(self._router.unique_name)[1:].replace(".", "_")
        path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        rule = MatchRule(
            type="signal",
            interface="org.freedesktop.portal.Request",
            member="Response",
            path=path,
        )
        self._bus_proxy.AddMatch(rule)
        try:
            with self._router.filter(rule) as responses:
                parameters = {**(options or {}), "handle_token": ("s", token)}
                (returned_path,) = getattr(self._remote, method)(*args, parameters, _timeout=5)
                if str(returned_path) != path:
                    raise RuntimeError(
                        "Desktop portal returned an unexpected permission-request path"
                    )
                # The router blocks on a queue serviced by its receiver thread;
                # this consumes no CPU while KDE waits for the user's choice.
                for _ in range(550):
                    if self._cancelled.is_set():
                        raise RuntimeError("Desktop input connection was cancelled")
                    try:
                        response = responses.get(timeout=0.1)
                        break
                    except Empty:
                        continue
                else:
                    raise TimeoutError(
                        "KDE desktop-control permission was not granted within 55 seconds"
                    )
                code, results = response.body
                if int(code) != 0:
                    raise RuntimeError("KDE desktop-control permission was cancelled or denied")
                return self._unvariant(dict(results))
        except BaseException:
            try:
                self._close_path(path, "org.freedesktop.portal.Request")
            except Exception:
                pass
            raise
        finally:
            try:
                self._bus_proxy.RemoveMatch(rule)
            except Exception:
                pass

    def connect(self) -> dict[str, Any]:
        if self._devices:
            return {"verified": True, **self.status()}
        self._cancelled.clear()
        self._ensure_connection()
        try:
            created = self._request(
                "CreateSession", options={"session_handle_token": ("s", "ev_" + uuid.uuid4().hex)}
            )
            self._session = str(created["session_handle"])
            self._start_session_watch()
            options: dict[str, Any] = {"types": ("u", 3)}
            (version_variant,) = self._properties.get("version", _timeout=3)
            version = int(self._unvariant(version_variant))
            if self.state_path is not None and version >= 2:
                options["persist_mode"] = ("u", 2)
                restore_token = self._load_restore_token()
                if restore_token:
                    options["restore_token"] = ("s", restore_token)
                    # Restore tokens are single use, including failed restores.
                    self._save_restore_token("")
            self._request("SelectDevices", self._session, options=options)
            started = self._request("Start", self._session, "")
            if self._cancelled.is_set() or not self._session:
                raise RuntimeError("Desktop input session closed before it was ready")
            self._devices = int(started.get("devices", 0))
            if self._devices & 3 != 3:
                raise RuntimeError("KDE did not grant both pointer and keyboard input")
            self._save_restore_token(str(started.get("restore_token", "")))
            return {"verified": True, **self.status()}
        except BaseException:
            self.close()
            raise

    def _start_session_watch(self) -> None:
        from jeepney.bus_messages import MatchRule

        self._stop_session_watch()
        rule = MatchRule(
            type="signal",
            interface="org.freedesktop.portal.Session",
            member="Closed",
            path=self._session,
        )
        self._bus_proxy.AddMatch(rule)
        self._closed_rule = rule
        self._closed_filter = self._router.filter(rule, bufsize=2)
        self._closed_queue = self._closed_filter.__enter__()

    def _poll_session_closed(self) -> None:
        if self._closed_queue is None:
            return
        try:
            self._closed_queue.get_nowait()
        except Empty:
            return
        self._session_closed()
        self._stop_session_watch()

    def _stop_session_watch(self) -> None:
        rule, handle = self._closed_rule, self._closed_filter
        self._closed_rule = None
        self._closed_filter = None
        self._closed_queue = None
        if rule is not None and self._bus_proxy is not None:
            try:
                self._bus_proxy.RemoveMatch(rule)
            except Exception:
                pass
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass

    def _close_path(self, path: str, interface: str) -> None:
        from jeepney.io.threading import Proxy
        from jeepney.wrappers import MessageGenerator, new_method_call

        class ClosableMessages(MessageGenerator):
            def Close(self) -> Any:
                return new_method_call(self, "Close")

        messages = ClosableMessages(path, self.SERVICE)
        messages.interface = interface
        Proxy(messages, self._router, timeout=3).Close()

    def _load_restore_token(self) -> str:
        if self.state_path is None or not self.state_path.is_file() or self.state_path.is_symlink():
            return ""
        try:
            token = json.loads(self.state_path.read_text(encoding="utf-8")).get("restore_token", "")
            return token if isinstance(token, str) and len(token) <= 4096 else ""
        except (OSError, ValueError, AttributeError):
            return ""

    def _save_restore_token(self, token: str) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", dir=self.state_path.parent, prefix=".ev-input-", delete=False
            ) as handle:
                temporary = Path(handle.name)
                os.fchmod(handle.fileno(), 0o600)
                json.dump({"restore_token": token}, handle)
            temporary.replace(self.state_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _session_closed(self, *_args: Any) -> None:
        self._session = ""
        self._devices = 0
        self._cancelled.set()

    def cancel(self) -> None:
        self._cancelled.set()

    def close(self) -> dict[str, Any]:
        self.cancel()
        session, self._session = self._session, ""
        self._devices = 0
        self._stop_session_watch()
        error = ""
        try:
            if session and self._router is not None:
                self._close_path(session, "org.freedesktop.portal.Session")
        except Exception as exception:
            error = str(exception)
        finally:
            context, self._router_context = self._router_context, None
            self._router = None
            self._bus_proxy = None
            self._remote = None
            self._properties = None
            if context is not None:
                try:
                    context.__exit__(None, None, None)
                except Exception as exception:
                    error = error or str(exception)
        return {
            "verified": not error,
            "connected": False,
            **({"error": error[:500]} if error else {}),
        }

    def notify(self, method: str, *args: Any) -> None:
        allowed = {
            "NotifyPointerMotion",
            "NotifyPointerButton",
            "NotifyPointerAxisDiscrete",
            "NotifyKeyboardKeysym",
        }
        if method not in allowed:
            raise ValueError("Unsupported desktop portal input method")
        self._poll_session_closed()
        required = 1 if method.startswith("NotifyKeyboard") else 2
        if not self._session or not self._devices & required:
            raise RuntimeError(
                "Desktop input is not connected for this device; run desktop.input.connect first"
            )
        try:
            getattr(self._remote, method)(self._session, {}, *args, _timeout=3)
        except Exception:
            # A timeout can happen after the compositor accepted input. Close
            # the still-live grant before dropping its only session path.
            self.close()
            raise


class DesktopInput:
    """Bounded input with current KWin target and pointer checks per operation."""

    KEYS = {
        "enter": 0xFF0D,
        "return": 0xFF0D,
        "tab": 0xFF09,
        "escape": 0xFF1B,
        "backspace": 0xFF08,
        "delete": 0xFFFF,
        "space": 0x20,
        "left": 0xFF51,
        "up": 0xFF52,
        "right": 0xFF53,
        "down": 0xFF54,
        "home": 0xFF50,
        "end": 0xFF57,
        "pageup": 0xFF55,
        "pagedown": 0xFF56,
        "insert": 0xFF63,
        "print": 0xFF61,
        "volume_down": 0x1008FF11,
        "volume_mute": 0x1008FF12,
        "volume_up": 0x1008FF13,
        "media_play": 0x1008FF14,
        "media_stop": 0x1008FF15,
        "media_previous": 0x1008FF16,
        "media_next": 0x1008FF17,
        "media_pause": 0x1008FF31,
        **{f"f{i}": 0xFFBD + i for i in range(1, 36)},
    }
    MODIFIERS = {
        "ctrl": 0xFFE3,
        "control": 0xFFE3,
        "shift": 0xFFE1,
        "alt": 0xFFE9,
        "super": 0xFFEB,
        "meta": 0xFFEB,
    }

    def __init__(self, desktop: Any, portal: Any = None, state_path: Path | None = None) -> None:
        self.desktop = desktop
        from ..platform import IS_FREEBSD

        if portal is None and IS_FREEBSD:
            from ..platform.x11_input import X11Input

            portal = X11Input(state_path)
        self.portal = portal or RemoteDesktopPortal(state_path)
        self._lock = asyncio.Lock()
        self._last_position_verified = 0.0

    def status(self) -> dict[str, Any]:
        status = self.portal.status()
        connected = bool(status.get("connected"))
        return {
            **status,
            "authorized": connected,
            "installed": importlib.util.find_spec("jeepney") is not None,
            "availability_evidence": "Python D-Bus dependency and session bus; connected requires a real portal grant",
            "functioning": (
                True
                if connected
                and self._last_position_verified
                and time.monotonic() - self._last_position_verified < 60
                else None
            ),
            "verification": {
                "pointer_position_observed": bool(connected and self._last_position_verified),
                "last_verified_monotonic": self._last_position_verified if connected else None,
                "application_effects_verified": False,
            },
        }

    def cancel_current(self) -> None:
        self.portal.cancel()

    def _raise_if_cancelled(self) -> None:
        cancelled = getattr(self.portal, "_cancelled", None)
        if cancelled is not None and cancelled.is_set():
            raise RuntimeError("Desktop input was cancelled")

    async def connect(self) -> dict[str, Any]:
        async with self._lock:
            try:
                self._last_position_verified = 0.0
                return await asyncio.to_thread(self.portal.connect)
            except asyncio.CancelledError:
                self.portal.cancel()
                raise

    async def close(self) -> dict[str, Any]:
        self.portal.cancel()
        async with self._lock:
            return await asyncio.to_thread(self.portal.close)

    async def _world(self) -> dict[str, Any]:
        # Pointer/focus checks do not need a kscreen-doctor process or cached
        # output metadata. Always read the live compositor in one bridge call.
        return await self.desktop.bridge.request("snapshot", {}, timeout=4)

    async def _target(self, window_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        world = await self._world()
        window = next(
            (item for item in world.get("windows", []) if str(item.get("id")) == window_id), None
        )
        if (
            not window
            or window.get("special")
            or window.get("minimized")
            or not (window.get("normal") or window.get("dialog"))
        ):
            raise RuntimeError("The exact target window is not available for input")
        if str(world.get("active_window_id", "")) != window_id:
            raise RuntimeError(
                "Target window lost focus; activate its exact window ID before sending input"
            )
        return world, window

    @staticmethod
    def _contains(rect: dict[str, Any], x: float, y: float) -> bool:
        return rect.get("x", 0) <= x < rect.get("x", 0) + rect.get("width", 0) and rect.get(
            "y", 0
        ) <= y < rect.get("y", 0) + rect.get("height", 0)

    def _point(self, world: dict[str, Any], window: dict[str, Any], x: float, y: float) -> None:
        if not self._contains(window.get("geometry", {}), x, y):
            raise RuntimeError("Pointer coordinates are outside the exact target window")
        if not any(
            self._contains(output.get("geometry", {}), x, y)
            for output in world.get("outputs", [])
            if output.get("enabled", True)
        ):
            raise RuntimeError("Pointer coordinates are outside enabled outputs")
        for other in world.get("windows", []):
            if other.get("id") == window.get("id") or other.get("minimized"):
                continue
            desktops = other.get("desktops", [])
            if (
                desktops
                and not other.get("on_all_desktops")
                and world.get("current_desktop") not in desktops
            ):
                continue
            if int(other.get("stacking_order", 0)) > int(
                window.get("stacking_order", 0)
            ) and self._contains(other.get("geometry", {}), x, y):
                raise RuntimeError(
                    "Another surface covers the target coordinates; refresh the screen before clicking"
                )

    async def _move(self, window_id: str, x: float, y: float) -> dict[str, Any]:
        for _ in range(4):
            self._raise_if_cancelled()
            world, window = await self._target(window_id)
            self._point(world, window, x, y)
            cursor = world.get("cursor", {})
            if "x" not in cursor or "y" not in cursor:
                raise RuntimeError("KWin did not report a verifiable pointer position")
            dx, dy = x - float(cursor["x"]), y - float(cursor["y"])
            if abs(dx) <= 1 and abs(dy) <= 1:
                self._last_position_verified = time.monotonic()
                return {"verified": True, "window_id": window_id, "cursor": cursor}
            await asyncio.to_thread(self.portal.notify, "NotifyPointerMotion", float(dx), float(dy))
            await asyncio.sleep(0.035)
        raise RuntimeError("Pointer did not reach the target; no click was sent")

    async def move(self, window_id: str, x: float, y: float) -> dict[str, Any]:
        async with self._lock:
            return await self._move(window_id, x, y)

    async def _press_release(self, method: str, keys: list[int]) -> None:
        pressed: list[int] = []
        try:
            for key in keys:
                self._raise_if_cancelled()
                pressed.append(key)
                # Release even if cancellation arrives while the D-Bus send is
                # running; never leave Ctrl or a pointer button held down.
                await self._send_ordered(method, key, 1)
        finally:
            await self._release_inputs([(method, key) for key in pressed])

    async def _send_ordered(self, method, *args):
        # A cancelled to_thread send can otherwise finish AFTER its release.
        # Wait for the bounded portal call to settle before releasing inputs.
        task = asyncio.create_task(asyncio.to_thread(self.portal.notify, method, *args))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(task)
            except Exception:
                pass
            raise

    async def _release_inputs(self, pressed):
        errors = []
        for method, key in reversed(pressed):
            try:
                await self._send_ordered(method, key, 0)
            except (Exception, asyncio.CancelledError) as error:
                errors.append(error)
        if errors:
            # Revoke the grant if a release couldn't be acknowledged. Try every
            # release first, so one failure cannot leave all other modifiers held.
            close = getattr(self.portal, "close", None)
            if close:
                await asyncio.shield(asyncio.to_thread(close))
            raise errors[0]

    @classmethod
    def _symbol(cls, key):
        if not isinstance(key, str) or not 1 <= len(key) <= 40:
            raise ValueError("Invalid keyboard key name")
        normalized = key.casefold()
        symbol = cls.KEYS.get(normalized, cls.MODIFIERS.get(normalized))
        if symbol is None and len(key) == 1 and key.isprintable():
            symbol = ord(key) if ord(key) <= 255 else 0x01000000 | ord(key)
        if symbol is None:
            # Resolve standard XKB key names locally; never evaluate input text.
            import ctypes
            import ctypes.util

            library = ctypes.util.find_library("xkbcommon")
            if library:
                lookup = ctypes.CDLL(library).xkb_keysym_from_name
                lookup.argtypes, lookup.restype = [ctypes.c_char_p, ctypes.c_int], ctypes.c_uint32
                symbol = lookup(key.encode("utf-8"), 1)
        if not symbol:
            raise ValueError("Unsupported key name")
        if (
            symbol in {0x1008FF10, 0x1008FF21, 0x1008FF2A, 0x1008FF2F, 0x1008FFA7, 0x1008FFA8}
            or 0x1008FE01 <= symbol <= 0x1008FE0C
        ):
            raise ValueError(
                "Session-ending and virtual-console keys require an explicit supported system action"
            )
        return symbol

    @classmethod
    def _check_chord(cls, symbols):
        if (
            cls.MODIFIERS["ctrl"] in symbols
            and cls.MODIFIERS["alt"] in symbols
            and any(
                s == cls.KEYS["delete"] or cls.KEYS["f1"] <= s <= cls.KEYS["f12"] for s in symbols
            )
        ):
            raise ValueError(
                "Session-ending and virtual-console shortcuts are not generic keyboard actions"
            )

    async def move_relative(self, window_id, dx, dy):
        if not all(
            type(v) in (int, float) and math.isfinite(v) and abs(v) <= 32768 for v in (dx, dy)
        ):
            raise ValueError("Invalid relative pointer distance")
        async with self._lock:
            world, _ = await self._target(window_id)
            cursor = world.get("cursor", {})
            return await self._move(window_id, float(cursor["x"]) + dx, float(cursor["y"]) + dy)

    async def gesture(self, window_id, events):
        """One bounded target-scoped sequence; held inputs never outlive it."""
        if not isinstance(events, list) or not 1 <= len(events) <= 64:
            raise ValueError("Gesture must contain 1-64 events")
        permitted = {
            "move": {"x", "y"},
            "relative": {"dx", "dy"},
            "button": {"button", "state"},
            "key": {"key", "state"},
            "pause": {"milliseconds"},
        }
        # Validate every event before emitting the first input.
        simulated = set()
        for event in events:
            if (
                not isinstance(event, dict)
                or event.get("type") not in permitted
                or set(event) != permitted[event["type"]] | {"type"}
            ):
                raise ValueError("Invalid gesture event")
            kind = event["type"]
            if kind in {"move", "relative"} and any(
                type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 32768
                for k, v in event.items()
                if k != "type"
            ):
                raise ValueError("Invalid pointer coordinate")
            if kind == "pause" and (
                type(event["milliseconds"]) is not int or not 1 <= event["milliseconds"] <= 250
            ):
                raise ValueError("Gesture pauses must be 1-250 milliseconds")
            if kind in {"key", "button"} and event["state"] not in {"press", "release"}:
                raise ValueError("Input state must be press or release")
            if kind == "key":
                self._symbol(event["key"])
            if kind == "button" and event["button"] not in {"left", "middle", "right"}:
                raise ValueError("Invalid mouse button")
            if kind in {"key", "button"}:
                pair = (kind, self._symbol(event["key"]) if kind == "key" else event["button"])
                if event["state"] == "press":
                    if pair in simulated:
                        raise ValueError("Input is already held by this gesture")
                    simulated.add(pair)
                elif pair not in simulated:
                    raise ValueError("Cannot release an input not held by this gesture")
                else:
                    simulated.remove(pair)
                self._check_chord({code for kind, code in simulated if kind == "key"})
        async with self._lock:
            initial, initial_window = await self._target(window_id)
            x, y = initial["cursor"]["x"], initial["cursor"]["y"]
            for event in events:
                if event["type"] == "move":
                    x, y = event["x"], event["y"]
                elif event["type"] == "relative":
                    x, y = x + event["dx"], y + event["dy"]
                if event["type"] in {"move", "relative", "button"}:
                    self._point(initial, initial_window, x, y)
            held, completed = [], 0
            deadline = time.monotonic() + 20
            try:
                for event in events:
                    self._raise_if_cancelled()
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Gesture time budget reached")
                    world, window = await self._target(window_id)
                    kind = event["type"]
                    if kind == "move":
                        await self._move(window_id, event["x"], event["y"])
                    elif kind == "relative":
                        await self._move(
                            window_id,
                            world["cursor"]["x"] + event["dx"],
                            world["cursor"]["y"] + event["dy"],
                        )
                    elif kind == "pause":
                        await asyncio.sleep(event["milliseconds"] / 1000)
                    else:
                        if kind == "button":
                            self._point(world, window, world["cursor"]["x"], world["cursor"]["y"])
                            method, code = (
                                "NotifyPointerButton",
                                {"left": 272, "right": 273, "middle": 274}[event["button"]],
                            )
                        else:
                            method, code = "NotifyKeyboardKeysym", self._symbol(event["key"])
                        pair = (method, code)
                        pressed = event["state"] == "press"
                        if pressed:
                            if pair in held:
                                raise ValueError("Input is already held by this gesture")
                            held.append(pair)
                        elif pair not in held:
                            raise ValueError("Cannot release an input not held by this gesture")
                        await self._send_ordered(method, code, int(pressed))
                        if not pressed:
                            held.remove(pair)
                    completed += 1
            finally:
                await self._release_inputs(held)
            return {
                "input_sent": True,
                "verified": False,
                "events_sent": completed,
                "window_id": window_id,
                "held_inputs_released": True,
                "verification_scope": "Input delivery and target checks, not application outcome",
            }

    async def click(
        self, window_id: str, x: float, y: float, button: str = "left", count: int = 1
    ) -> dict[str, Any]:
        if button not in {"left", "middle", "right"} or count not in {1, 2}:
            raise ValueError("Unsupported pointer button or click count")
        async with self._lock:
            await self._move(window_id, x, y)
            for _ in range(count):
                self._raise_if_cancelled()
                world, window = await self._target(window_id)
                self._point(world, window, x, y)
                cursor = world.get("cursor", {})
                if (
                    abs(float(cursor.get("x", -100000)) - x) > 1
                    or abs(float(cursor.get("y", -100000)) - y) > 1
                ):
                    raise RuntimeError("Pointer moved before clicking; no further clicks were sent")
                await self._press_release(
                    "NotifyPointerButton", [{"left": 272, "right": 273, "middle": 274}[button]]
                )
            after = await self._world()
            return {
                "verified": False,
                "input_sent": True,
                "window_id": window_id,
                "clicks": count,
                "active_window_id": after.get("active_window_id"),
                "verification_scope": "Target focus and pointer checked before input; inspect screenshot or accessibility to verify application outcome",
            }

    async def scroll(
        self, window_id: str, x: float, y: float, direction: str, steps: int = 3
    ) -> dict[str, Any]:
        if direction not in {"up", "down", "left", "right"} or not 1 <= steps <= 20:
            raise ValueError("Invalid scroll direction or number of steps")
        async with self._lock:
            await self._move(window_id, x, y)
            self._raise_if_cancelled()
            world, window = await self._target(window_id)
            self._point(world, window, x, y)
            await asyncio.to_thread(
                self.portal.notify,
                "NotifyPointerAxisDiscrete",
                0 if direction in {"up", "down"} else 1,
                -steps if direction in {"up", "left"} else steps,
            )
            return {
                "verified": False,
                "input_sent": True,
                "window_id": window_id,
                "direction": direction,
                "steps": steps,
                "verification_scope": "Input delivery only; inspect visible content to verify scrolling",
            }

    async def key(
        self, window_id: str, key: str, modifiers: list[str] | None = None
    ) -> dict[str, Any]:
        symbol = self._symbol(key)
        modifier_keys = []
        for modifier in modifiers or []:
            if modifier.casefold() not in self.MODIFIERS:
                raise ValueError("Unsupported keyboard modifier")
            value = self.MODIFIERS[modifier.casefold()]
            if value not in modifier_keys:
                modifier_keys.append(value)
        self._check_chord({*modifier_keys, symbol})
        async with self._lock:
            self._raise_if_cancelled()
            await self._target(window_id)
            await self._press_release("NotifyKeyboardKeysym", [*modifier_keys, symbol])
            after = await self._world()
            return {
                "verified": False,
                "input_sent": True,
                "window_id": window_id,
                "active_window_id": after.get("active_window_id"),
                "verification_scope": "Key delivery only; verify the requested app outcome separately",
            }

    async def type_text(self, window_id: str, text: str, *, control_guard=None) -> dict[str, Any]:
        if not text or len(text) > 2000 or any(not character.isprintable() for character in text):
            raise ValueError(
                "Keyboard text must contain 1-2000 printable characters; send Enter or Tab as an explicit key action"
            )
        async with self._lock:
            sent = 0
            for offset in range(0, len(text), 8):
                self._raise_if_cancelled()
                await self._target(window_id)
                if control_guard is not None:
                    await control_guard(offset)
                    self._raise_if_cancelled()
                    await self._target(window_id)
                for character in text[offset : offset + 8]:
                    symbol = (
                        ord(character) if ord(character) <= 255 else 0x01000000 | ord(character)
                    )
                    await self._press_release("NotifyKeyboardKeysym", [symbol])
                    sent += 1
            await self._target(window_id)
            return {
                "verified": False,
                "input_sent": True,
                "window_id": window_id,
                "characters": sent,
                "submitted": False,
                "verification_scope": "Key delivery with focus checks every 8 characters; use AT-SPI readback or OCR to verify text. Keysyms depend on the compositor keyboard layout.",
            }
