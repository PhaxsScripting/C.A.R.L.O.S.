#!/usr/bin/env python3
"""Opt-in: capture only an owned disposable GTK window and describe it locally."""

import argparse
import asyncio
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
from ev.ai.local_vision import LocalVisualReasoner
from ev.config import DEFAULT_CONFIG
from ev.paths import get_paths


def child(title):
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    window = Gtk.Window(title=title)
    window.set_default_size(520, 280)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
    box.set_border_width(30)
    box.pack_start(Gtk.Label(label="Download failed"), False, False, 0)
    box.pack_start(Gtk.Label(label="Network connection lost."), False, False, 0)
    row = Gtk.Box(spacing=20)
    row.pack_start(Gtk.Button(label="Retry"), True, True, 0)
    row.pack_start(Gtk.Button(label="Cancel"), True, True, 0)
    box.pack_start(row, False, False, 0)
    window.add(box)
    window.connect("destroy", Gtk.main_quit)
    window.show_all()
    Gtk.main()


async def run():
    title = "E.V. local vision fixture " + uuid.uuid4().hex[:12]
    process = subprocess.Popen([sys.executable, __file__, "--child", title])
    capture_id = None

    async def tool(name, arguments):
        result = await asyncio.to_thread(
            subprocess.run,
            [
                str(Path.home() / ".local/bin/evctl"),
                "tool",
                name,
                "--arguments",
                json.dumps(arguments),
            ],
            capture_output=True,
            text=True,
            timeout=25,
        )
        payload = json.loads(result.stdout)["payload"]
        if payload.get("status") != "completed":
            raise RuntimeError(f"Fixture {name} failed: {payload}")
        return payload["result"]

    try:
        # General app resolution is fuzzy by design; a fixture must instead
        # wait for its exact new PID/title and ignore any stale earlier window.
        deadline = time.monotonic() + 8
        window = None
        while time.monotonic() < deadline:
            world = await tool("desktop.world", {})
            window = next(
                (
                    w
                    for w in world.get("windows", [])
                    if w.get("pid") == process.pid and w.get("title") == title
                ),
                None,
            )
            if window:
                break
            await asyncio.sleep(0.15)
        if window is None:
            raise RuntimeError("Exact fixture identity did not appear; no capture taken")
        await tool("desktop.window.activate", {"window_id": window["id"]})
        capture = await tool("vision.capture", {"window_id": window["id"]})
        capture_id = capture["capture_id"]
        paths = get_paths()
        reasoner = LocalVisualReasoner(
            {**DEFAULT_CONFIG["vision"]["local_model"], "enabled": True},
            paths.runtime_dir / "live-vision-fixture-owner.json",
        )
        result = await reasoner.describe(
            Path(capture["path"]),
            "What error message and buttons are visible? Describe only; do not act.",
        )
        text = result["description"].casefold()
        passed = all(w in text for w in ("download", "network", "retry", "cancel"))
        print(
            json.dumps(
                {
                    **result,
                    "owned_window_only": True,
                    "synthetic_word_check": passed,
                    "model_actions_executed": 0,
                    "cloud_requests": 0,
                }
            ),
            flush=True,
        )
        return passed
    finally:
        try:
            if capture_id:
                await tool("vision.capture.delete", {"capture_id": capture_id})
        finally:
            if process.poll() is None:
                process.terminate()
            await asyncio.to_thread(process.wait, timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--child", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        child(args.child)
    elif args.run:
        sys.exit(0 if asyncio.run(run()) else 1)
    else:
        parser.error("Pass --run to inspect a disposable test window")
