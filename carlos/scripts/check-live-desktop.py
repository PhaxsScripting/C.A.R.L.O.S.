#!/usr/bin/env python3
"""Opt-in live smoke check. Only manipulates its own disposable GTK window."""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

TITLE = "E.V. disposable reliability check"
MARKER = "E.V. keyboard verified"


def window_child() -> None:
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    window = Gtk.Window(title=TITLE)
    window.set_default_size(520, 180)
    entry = Gtk.Entry()
    entry.set_placeholder_text("Temporary E.V. input check; closes automatically")
    entry.connect(
        "changed", lambda widget: print(json.dumps({"text": widget.get_text()}), flush=True)
    )
    window.add(entry)
    window.connect("destroy", Gtk.main_quit)
    window.show_all()
    entry.grab_focus()
    Gtk.main()


def check(window_only: bool = False) -> None:
    evctl = str(Path.home() / ".local/bin/evctl")

    def tool(name: str, arguments: dict) -> dict:
        response = subprocess.run(
            [evctl, "tool", name, "--arguments", json.dumps(arguments)],
            capture_output=True,
            text=True,
            timeout=65 if name == "desktop.input.connect" else 15,
        )
        if response.returncode:
            raise RuntimeError(f"{name}: {response.stdout or response.stderr}")
        payload = json.loads(response.stdout)["payload"]
        if payload.get("status") != "completed":
            raise RuntimeError(f"{name}: {payload}")
        return payload["result"]

    child = subprocess.Popen([sys.executable, __file__, "--window-child"], stdout=subprocess.PIPE)
    try:
        result = tool("desktop.window.wait", {"description": TITLE, "timeout_seconds": 8})
        assert result.get("resolved"), result
        window = result["window"]
        assert (
            int(window["pid"]) == child.pid
        ), "Refusing to manipulate a window not owned by this test"
        window_id = window["id"]
        for state in ("minimize", "restore"):
            result = tool("desktop.window.state", {"window_id": window_id, "state": state})
            assert result.get("verified"), result
            print(json.dumps({"state": state, "verified": True}), flush=True)
        for layout in ("top-right", "bottom-left"):
            result = tool("desktop.window.layout", {"window_id": window_id, "layout": layout})
            assert result.get("verified"), result
            print(
                json.dumps({"layout": layout, "verified": True, "geometry": result.get("actual")}),
                flush=True,
            )
        if window_only:
            assert tool("desktop.window.close", {"window_id": window_id}).get("verified")
            print(
                json.dumps({"exact_window_close_verified": True, "keyboard_input_sent": False}),
                flush=True,
            )
            return
        # Exercise the actual ordered planner. Separate IPC tool calls settle
        # the UI between activation and typing and can change desktop focus.
        phrase = f'focus window-id:{window_id} then type "{MARKER}"'
        response = subprocess.run(
            [evctl, "ask", phrase], capture_output=True, text=True, timeout=85
        )
        if response.returncode:
            raise RuntimeError(response.stdout or response.stderr)
        command = json.loads(response.stdout)["payload"]
        assert command.get("status") == "completed", command
        steps = command["plan"]["steps"]
        typed = [step for step in steps if step["tool"] == "desktop.keyboard.type_text"]
        assert len(typed) == 1 and typed[0]["resolved_arguments"]["window_id"] == window_id
        assert typed[0]["actual_result"]["result"].get("input_sent"), typed
        deadline = time.monotonic() + 5
        observed = ""
        pending = b""
        assert child.stdout is not None
        while time.monotonic() < deadline:
            if select.select([child.stdout], [], [], max(0, deadline - time.monotonic()))[0]:
                chunk = os.read(child.stdout.fileno(), 4096)
                if not chunk:
                    break
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    observed = json.loads(line).get("text", "")
                if observed == MARKER:
                    break
        assert observed == MARKER, f"Typed text did not match: {observed!r}"
        print(json.dumps({"keyboard_readback_verified": True}), flush=True)
        result = tool("desktop.window.close", {"window_id": window_id})
        assert result.get("verified"), result
        print(json.dumps({"exact_window_close_verified": True}), flush=True)
    finally:
        if child.poll() is None:
            child.terminate()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=3)


if __name__ == "__main__":
    if sys.argv[1:] == ["--window-child"]:
        window_child()
    elif sys.argv[1:] == ["--run"]:
        check()
    elif sys.argv[1:] == ["--window-only"]:
        check(window_only=True)
    else:
        raise SystemExit("Pass --run to briefly show and test a disposable window.")
