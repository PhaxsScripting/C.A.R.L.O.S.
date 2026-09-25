"""User-session XTest adapter for the existing bounded DesktopInput protocol."""

from __future__ import annotations
import ctypes
import ctypes.util
import os
import threading


class X11Input:
    def __init__(self, state_path=None):
        self._display = None
        self._cancelled = threading.Event()
        self._lock = threading.RLock()
        self._buttons = set()
        self._keys = set()
        self._x = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so")
        self._t = ctypes.CDLL(ctypes.util.find_library("Xtst") or "libXtst.so")
        self._x.XInitThreads()
        self._x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self._x.XOpenDisplay.restype = ctypes.c_void_p
        self._x.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self._x.XFlush.argtypes = [ctypes.c_void_p]
        self._x.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self._x.XKeysymToKeycode.restype = ctypes.c_ubyte
        self._t.XTestFakeRelativeMotionEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        self._t.XTestFakeButtonEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        self._t.XTestFakeKeyEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.c_ulong,
        ]

    def status(self):
        return {
            "available": bool(os.environ.get("DISPLAY")),
            "connected": bool(self._display),
            "keyboard": bool(self._display),
            "pointer": bool(self._display),
            "backend": "freebsd-x11-xtest",
            "session_consent_required": False,
            "network_exposed": False,
            "reason": "X11 user-session input; existing E.V. tool confirmations still apply",
        }

    def connect(self):
        with self._lock:
            if not self._display:
                self._display = self._x.XOpenDisplay(None)
            if not self._display:
                raise RuntimeError("Cannot open current X11 display")
            self._cancelled.clear()
            return self.status()

    def cancel(self):
        self._cancelled.set()

    def close(self):
        with self._lock:
            self._cancelled.set()
            if self._display:
                for b in self._buttons:
                    self._t.XTestFakeButtonEvent(self._display, b, 0, 0)
                for k in self._keys:
                    self._t.XTestFakeKeyEvent(self._display, k, 0, 0)
                self._x.XFlush(self._display)
                self._x.XCloseDisplay(self._display)
            self._display = None
            self._buttons.clear()
            self._keys.clear()
            return self.status()

    def notify(self, method, *args):
        with self._lock:
            if not self._display:
                raise RuntimeError("Connect X11 input before use")
            # Releases remain possible during cancellation; shared caller guarantees cleanup.
            release = (
                method in {"NotifyPointerButton", "NotifyKeyboardKeysym"} and int(args[1]) == 0
            )
            if self._cancelled.is_set() and not release:
                raise RuntimeError("Input cancelled")
            if method == "NotifyPointerMotion":
                self._t.XTestFakeRelativeMotionEvent(
                    self._display, round(args[0]), round(args[1]), 0
                )
            elif method == "NotifyPointerButton":
                b = {272: 1, 273: 3, 274: 2}.get(int(args[0]))
                pressed = int(args[1])
                if b is None:
                    raise ValueError("Unsupported button")
                self._t.XTestFakeButtonEvent(self._display, b, pressed, 0)
                (self._buttons.add if pressed else self._buttons.discard)(b)
            elif method == "NotifyKeyboardKeysym":
                k = self._x.XKeysymToKeycode(self._display, int(args[0]))
                pressed = int(args[1])
                if not k:
                    raise ValueError("Key unavailable in current keyboard layout")
                self._t.XTestFakeKeyEvent(self._display, k, pressed, 0)
                (self._keys.add if pressed else self._keys.discard)(k)
            elif method == "NotifyPointerAxisDiscrete":
                axis, steps = int(args[0]), int(args[1])
                b = (4 if steps < 0 else 5) if axis == 0 else (6 if steps < 0 else 7)
                for _ in range(min(abs(steps), 100)):
                    self._t.XTestFakeButtonEvent(self._display, b, 1, 0)
                    self._t.XTestFakeButtonEvent(self._display, b, 0, 0)
            else:
                raise ValueError("Unsupported input method")
            self._x.XFlush(self._display)
