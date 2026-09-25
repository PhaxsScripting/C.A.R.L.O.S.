#!/usr/bin/env python3
"""Opt-in checks on an owned disposable GTK window, with independent readback."""

import argparse
import json
import os
import select
import subprocess
import sys
import time
import uuid
from pathlib import Path

TITLE = "E.V. disposable semantic controls"
PICKER_TITLE = "E.V. disposable file chooser"
PICKER_FILE = Path(__file__).resolve().parents[1] / "tests/fixtures/picker-candidate.txt"


def child():
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, Gdk

    window = Gtk.Window(title=TITLE)
    window.set_default_size(460, 300)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    check = Gtk.CheckButton(label="E.V. test option")
    check.get_accessible().set_name("E.V. test option")
    check.connect(
        "toggled", lambda widget: print(json.dumps({"checked": widget.get_active()}), flush=True)
    )
    scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
    scale.get_accessible().set_name("E.V. test slider")
    scale.connect(
        "value-changed", lambda widget: print(json.dumps({"value": widget.get_value()}), flush=True)
    )
    box.pack_start(check, False, False, 10)
    box.pack_start(scale, False, False, 10)
    entry = Gtk.Entry()
    entry.get_accessible().set_name("E.V. test field")
    entry.set_text("unchanged fixture")
    entry.connect(
        "changed", lambda widget: print(json.dumps({"text": widget.get_text()}), flush=True)
    )
    box.pack_start(entry, False, False, 10)
    choose = Gtk.Button(label="Choose fixture file")
    choose.get_accessible().set_name("Choose fixture file")

    def show_picker(_button):
        dialog = Gtk.FileChooserDialog(
            title=PICKER_TITLE + TITLE, parent=window, action=Gtk.FileChooserAction.OPEN
        )
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Open", Gtk.ResponseType.OK)
        dialog.set_local_only(True)
        dialog.set_current_folder(str(PICKER_FILE.parent))

        def selected(widget, response):
            if response == Gtk.ResponseType.OK:
                print(json.dumps({"selected_file": widget.get_filename()}), flush=True)
            widget.destroy()

        dialog.connect("response", selected)
        dialog.show_all()
        print(
            json.dumps({"picker_role": dialog.get_accessible().get_role().value_nick}), flush=True
        )

    choose.connect("clicked", show_picker)
    box.pack_start(choose, False, False, 10)
    surface = Gtk.DrawingArea()
    surface.set_size_request(400, 150)
    surface.add_events(
        Gdk.EventMask.BUTTON_PRESS_MASK
        | Gdk.EventMask.BUTTON_RELEASE_MASK
        | Gdk.EventMask.POINTER_MOTION_MASK
    )
    surface.connect(
        "button-press-event",
        lambda widget, event: print(
            json.dumps({"pointer": "pressed", "button": event.button}), flush=True
        )
        or False,
    )
    surface.connect(
        "button-release-event",
        lambda widget, event: print(
            json.dumps({"pointer": "released", "button": event.button}), flush=True
        )
        or False,
    )
    box.pack_start(surface, True, True, 0)
    window.add(box)
    window.connect("destroy", Gtk.main_quit)
    window.show_all()
    Gtk.main()


def run(gestures=False, layout=False, picker=False, local_model=None):
    global TITLE
    TITLE = TITLE + " " + uuid.uuid4().hex[:12]
    executable = str(Path.home() / ".local/bin/evctl")

    def tool(name, arguments):
        response = subprocess.run(
            [executable, "tool", name, "--arguments", json.dumps(arguments)],
            capture_output=True,
            text=True,
            timeout=65 if name == "desktop.input.connect" else 30,
        )
        payload = json.loads(response.stdout)["payload"]
        if payload.get("status") != "completed":
            if name != "desktop.world":
                try:
                    world = tool("desktop.world", {})
                    active = next(
                        (
                            w
                            for w in world.get("windows", [])
                            if w.get("id") == world.get("active_window_id")
                        ),
                        {},
                    )
                    print(
                        json.dumps(
                            {
                                "failure_focus": {
                                    key: active.get(key) for key in ("id", "pid", "app_id")
                                }
                            }
                        ),
                        flush=True,
                    )
                except Exception:
                    pass
            raise RuntimeError(f"{name}: {payload}")
        return payload["result"]

    process = subprocess.Popen(
        [sys.executable, __file__, "--child", "--title", TITLE], stdout=subprocess.PIPE, bufsize=0
    )
    pending, buffered = [], b""

    def records(timeout=0.1):
        nonlocal buffered
        if pending:
            return pending.pop(0)
        ready, _, _ = select.select([process.stdout], [], [], timeout)
        if not ready:
            return None
        buffered += os.read(process.stdout.fileno(), 4096)
        while b"\n" in buffered:
            line, buffered = buffered.split(b"\n", 1)
            pending.append(json.loads(line))
        return pending.pop(0) if pending else None

    window_id, layout_name = "", ""
    try:
        observed = tool("desktop.window.wait", {"description": TITLE, "timeout_seconds": 8})
        window = observed["window"]
        if int(window["pid"]) != process.pid:
            raise RuntimeError("Refusing to manipulate a window not owned by this test")
        window_id = window["id"]
        tool("desktop.window.activate", {"window_id": window_id})
        if local_model:
            import asyncio
            from local_gui_fixture import run_local_picker

            model_completed = asyncio.run(
                run_local_picker(local_model, window_id, process.pid, TITLE, PICKER_FILE)
            )
            selected, deadline = None, time.monotonic() + 3
            while time.monotonic() < deadline:
                record = records()
                if record and "selected_file" in record:
                    selected = record["selected_file"]
                    break
            passed = model_completed and selected == str(PICKER_FILE)
            print(
                json.dumps(
                    {
                        "local_model_picker": "LIVE_VERIFIED" if passed else "FAILED",
                        "independent_selection_matches": selected == str(PICKER_FILE),
                        "model_completed": model_completed,
                    }
                ),
                flush=True,
            )
            if not passed:
                raise RuntimeError(
                    "Local model did not complete the independently observed file-picker goal"
                )
            return
        if layout:
            original = next(w for w in tool("desktop.world", {})["windows"] if w["id"] == window_id)
            layout_name = "ev-disposable-" + uuid.uuid4().hex
            tool("workspaces.capture", {"name": layout_name, "window_ids": [window_id]})
            tool("desktop.window.state", {"window_id": window_id, "state": "fullscreen"})
            plan = tool("workspaces.restore_plan", {"name": layout_name})
            restored = tool("agent.execute_plan", plan["plan"])
            actual = next(w for w in tool("desktop.world", {})["windows"] if w["id"] == window_id)
            if (
                not restored.get("verified")
                or actual["fullscreen"]
                or actual["minimized"]
                or any(abs(actual["geometry"][k] - v) > 2 for k, v in original["geometry"].items())
            ):
                raise RuntimeError(
                    "Disposable workspace layout did not restore its observed original geometry/state"
                )
            print(
                json.dumps(
                    {
                        "workspace_layout_restore": "LIVE_VERIFIED",
                        "exact_session_restoration": False,
                    }
                ),
                flush=True,
            )
        # Diagnostic tools can settle the HUD between calls; refresh exact focus
        # for each operation, never redirect to a new active application.
        for name, role, state, value, output_key in (
            ("E.V. test option", "check box", "checked", True, "checked"),
            ("E.V. test slider", "slider", "value", 37, "value"),
        ):
            listing = tool(
                "desktop.controls.list", {"window_id": window_id, "query": name, "role": role}
            )
            matches = [
                e
                for e in listing.get("items", [])
                if e.get("name") == name and e.get("role") == role
            ]
            if listing.get("truncated") or len(matches) != 1:
                raise RuntimeError(f"Owned control not uniquely exposed by AT-SPI: {name}")
            element = matches[0]
            identity = {key: element[key] for key in ("application", "name", "role")}
            identity["path"] = element["id"]
            identity.update(window_id=window_id, process_id=process.pid, window_title=TITLE)
            tool("desktop.window.activate", {"window_id": window_id})
            result = tool(
                "agent.execute_plan",
                {
                    "goal": "Set the owned disposable test control",
                    "steps": [
                        {
                            "id": "focus",
                            "tool": "desktop.window.activate",
                            "arguments": {"window_id": window_id},
                        },
                        {
                            "id": "set",
                            "tool": "desktop.controls.set_state",
                            "arguments": {**identity, "state": state, "value": value},
                        },
                    ],
                    "conditions": [
                        {
                            "kind": "control_state",
                            "target": identity,
                            "property": state,
                            "expected": value,
                        }
                    ],
                },
            )
            if not result.get("verified"):
                raise RuntimeError(f"Control readback was not verified: {result}")
            deadline, actual = time.monotonic() + 3, None
            while time.monotonic() < deadline:
                record = records()
                if record and output_key in record:
                    actual = record[output_key]
                    if actual == value:
                        break
            if actual != value:
                raise RuntimeError(f"Independent GTK readback disagrees: {actual!r} != {value!r}")
            print(
                json.dumps(
                    {"control": role, "backend_verified": True, "independent_widget_readback": True}
                ),
                flush=True,
            )
        listing = tool(
            "desktop.controls.list", {"window_id": window_id, "query": "E.V. test field"}
        )
        matches = [e for e in listing.get("items", []) if e.get("name") == "E.V. test field"]
        if listing.get("truncated") or len(matches) != 1:
            raise RuntimeError("Owned text field not uniquely exposed")
        element = matches[0]
        identity = {key: element[key] for key in ("application", "name", "role")}
        identity.update(
            path=element["id"], window_id=window_id, process_id=process.pid, window_title=TITLE
        )
        tool("desktop.window.activate", {"window_id": window_id})
        inspected = tool("desktop.controls.inspect", identity)
        if not inspected.get("text_available") or inspected.get("text_truncated"):
            raise RuntimeError("Owned field text was not completely observable")
        desired = "Generic field ✓ " + uuid.uuid4().hex[:8]
        plan = tool(
            "agent.execute_plan",
            {
                "goal": "Fill owned unfamiliar-app field and independently observe exact text",
                "steps": [
                    {
                        "id": "focus",
                        "tool": "desktop.window.activate",
                        "arguments": {"window_id": window_id},
                    },
                    {
                        "id": "field",
                        "tool": "desktop.controls.set_text",
                        "arguments": {
                            **identity,
                            "expected_text": inspected["text"],
                            "text": desired,
                        },
                    },
                ],
                "conditions": [{"kind": "control_text", "target": identity, "expected": desired}],
            },
        )
        if not plan.get("verified"):
            raise RuntimeError("Exact field-text condition not verified")
        deadline, actual = time.monotonic() + 3, None
        while time.monotonic() < deadline:
            record = records()
            if record and record.get("text") == desired:
                actual = record["text"]
                break
        if actual != desired:
            raise RuntimeError("Independent GTK field readback disagrees")
        print(
            json.dumps(
                {
                    "generic_editable_field": "LIVE_VERIFIED",
                    "exact_text_goal": True,
                    "form_submitted": False,
                }
            ),
            flush=True,
        )
        if picker:

            def control(wid, pid, title, name="", role="", editable=False):
                listed = tool(
                    "desktop.controls.list", {"window_id": wid, "query": name, "role": role}
                )
                matches = [
                    e
                    for e in listed.get("items", [])
                    if (not name or e["name"] == name) and (not editable or e.get("editable_text"))
                ]
                if listed.get("truncated") or len(matches) != 1:
                    raise RuntimeError(f"Picker control ambiguous: {listed}")
                e = matches[0]
                return {
                    **{k: e[k] for k in ("application", "name", "role")},
                    "path": e["id"],
                    "window_id": wid,
                    "process_id": pid,
                    "window_title": title,
                }

            choose_identity = control(window_id, process.pid, TITLE, "Choose fixture file")
            tool("desktop.controls.activate", choose_identity)
            dialog_title = PICKER_TITLE + TITLE
            dialog = tool(
                "desktop.window.wait", {"description": dialog_title, "timeout_seconds": 8}
            )["window"]
            if dialog["pid"] != process.pid or dialog["id"] == window_id:
                raise RuntimeError("File picker is not an exact owned child dialog")
            dialog_id = dialog["id"]
            record = records(2)
            print(json.dumps({"picker_creation": record}), flush=True)
            tool("desktop.window.activate", {"window_id": dialog_id})
            file_identity = control(
                dialog_id, process.pid, dialog_title, PICKER_FILE.name, "table cell"
            )
            try:
                selection = tool(
                    "desktop.controls.set_state",
                    {**file_identity, "state": "selected", "value": True},
                )
            except RuntimeError:
                # Read-only diagnostic confined to the exact owned fixture.
                sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
                from ev.accessibility import AccessibilityBridge

                bridge = AccessibilityBridge()
                node, _ = bridge._resolve_node(
                    file_identity["application"],
                    file_identity["name"],
                    file_identity["role"],
                    process.pid,
                    dialog_title,
                )
                table = bridge.Atspi.TableCell.get_table(node)
                coordinates = bridge.Atspi.TableCell.get_row_column_span(node)
                observed = bridge.Atspi.Table.get_accessible_at(
                    table, coordinates[0], coordinates[1]
                )
                children = (
                    []
                    if observed is None
                    else [
                        observed.get_child_at_index(i)
                        for i in range(min(8, observed.get_child_count()))
                    ]
                )
                print(
                    json.dumps(
                        {
                            "table_diagnostic": {
                                "coordinates": coordinates,
                                "position": bridge.Atspi.TableCell.get_position(node),
                                "target": node.path,
                                "observed": getattr(observed, "path", None),
                                "children": [getattr(c, "path", None) for c in children],
                            }
                        }
                    ),
                    flush=True,
                )
                raise
            fresh = tool("desktop.controls.inspect", file_identity)
            if (
                not selection.get("verified")
                or fresh.get("element", {}).get("states", {}).get("selected") is not True
            ):
                raise RuntimeError("Refusing Open: exact fixture row selection was not verified")
            open_identity = control(dialog_id, process.pid, dialog_title, "Open", "button")
            tool("desktop.controls.activate", open_identity)
            deadline, selected = time.monotonic() + 5, None
            while time.monotonic() < deadline:
                record = records()
                if record and "selected_file" in record:
                    selected = record["selected_file"]
                    break
            if selected != str(PICKER_FILE):
                raise RuntimeError(f"Independent file chooser readback disagrees: {selected!r}")
            print(
                json.dumps(
                    {
                        "native_file_picker": "LIVE_VERIFIED",
                        "independent_callback": True,
                        "adapter": "generic observed accessibility controls",
                        "keyboard_injection": False,
                        "model_goal_planning": "UNVERIFIED",
                    }
                ),
                flush=True,
            )
            tool("desktop.window.activate", {"window_id": window_id})
        if gestures:
            status = tool("desktop.input.status", {})
            if not status.get("connected"):
                print(
                    "Connecting native input for the disposable gesture test; KDE may require your approval.",
                    flush=True,
                )
                tool("desktop.input.connect", {})
            world = tool("desktop.world", {})
            current = next(w for w in world["windows"] if w["id"] == window_id)
            geometry = current["geometry"]
            x, y = geometry["x"] + geometry["width"] / 2, geometry["y"] + geometry["height"] - 45
            result = tool(
                "agent.execute_plan",
                {
                    "goal": "Test a pointer gesture in the owned drawing area",
                    "steps": [
                        {
                            "id": "focus",
                            "tool": "desktop.window.activate",
                            "arguments": {"window_id": window_id},
                        },
                        {
                            "id": "drag",
                            "tool": "desktop.pointer.drag",
                            "arguments": {
                                "window_id": window_id,
                                "start_x": x,
                                "start_y": y,
                                "end_x": x + 50,
                                "end_y": y,
                            },
                        },
                    ],
                    "conditions": [{"kind": "window_active", "window_id": window_id}],
                },
            )
            deadline, events = time.monotonic() + 3, []
            while time.monotonic() < deadline and len(events) < 2:
                record = records()
                if record and "pointer" in record:
                    events.append(record["pointer"])
            if events != ["pressed", "released"]:
                raise RuntimeError(f"Independent drawing-area input readback failed: {events}")
            print(
                json.dumps(
                    {
                        "gesture_press_release": "LIVE_VERIFIED",
                        "arbitrary_drag_drop_outcome": "UNVERIFIED",
                    }
                ),
                flush=True,
            )
    finally:
        if layout_name:
            try:
                tool("workspaces.remove", {"name": layout_name})
            except Exception as error:
                print(f"Disposable layout metadata cleanup failed: {error}", file=sys.stderr)
        if process.poll() is None:
            if window_id:
                try:
                    tool("desktop.window.close", {"window_id": window_id})
                except Exception:
                    pass
            if process.poll() is None:
                process.terminate()  # Exact child created by this script only.
            process.wait(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--title", default=TITLE, help=argparse.SUPPRESS)
    parser.add_argument(
        "--gestures",
        action="store_true",
        help="Also test a scoped native pointer gesture; KDE input consent may be required",
    )
    parser.add_argument(
        "--layout",
        action="store_true",
        help="Also test saved-layout restoration of the owned window, then remove its test metadata",
    )
    parser.add_argument(
        "--picker",
        action="store_true",
        help="Select a repository fixture through an owned native file chooser using generic accessibility",
    )
    parser.add_argument(
        "--local-model",
        help="Instead let a real local GGUF plan the complete chooser task; fixture-only guard, no cloud",
    )
    args = parser.parse_args()
    if args.child:
        TITLE = args.title
        child()
    elif args.run:
        run(args.gestures, args.layout, args.picker, args.local_model)
    else:
        parser.error("Pass --run to test a disposable window")
