#!/usr/bin/env python3
"""Verify document URL through the installed core using owned Firefox + local page.

Creates a disposable browser profile, never reads the user's browser profile.
No model requests. Only fixture windows are activated/closed. Browser startup
itself may contact browser-vendor services; no account credentials are loaded.
"""

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from urllib.parse import urlencode
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
from ev.cli import request


def diagnose_owned_document(pid, title):
    from collections import deque
    from ev.accessibility import AccessibilityBridge

    bridge = AccessibilityBridge()
    queue = deque()
    for app in bridge._applications():
        if bridge._process_id(app) == pid:
            queue.extend((root, 0) for root in bridge._window_roots(app, title))
    seen, rows, overview = set(), [], []
    while queue and len(seen) < 1500:
        node, depth = queue.popleft()
        path = str(getattr(node, "path", ""))
        identity = (str(getattr(getattr(node, "app", None), "bus_name", "")), path)
        if identity in seen:
            continue
        seen.add(identity)
        try:
            role = node.get_role_name()
            if len(overview) < 80:
                overview.append(
                    {
                        "role": role,
                        "name": str(node.get_name())[:80],
                        "depth": depth,
                        "bus": identity[0],
                        "path": path,
                    }
                )
            if "document" in role or node.is_document():
                states = node.get_state_set()
                row = {
                    "role": role,
                    "depth": depth,
                    "showing": bool(states.contains(bridge.Atspi.StateType.SHOWING)),
                    "busy": bool(states.contains(bridge.Atspi.StateType.BUSY)),
                }
                row["interfaces"] = node.get_interfaces()
                row["accessible_attributes"] = node.get_attributes()
                try:
                    row["document_attributes"] = bridge.Atspi.Document.get_document_attributes(node)
                except Exception as error:
                    row["attribute_error"] = str(error)
                rows.append(row)
            if depth < 24:
                queue.extend(
                    (node.get_child_at_index(i), depth + 1)
                    for i in range(min(node.get_child_count(), 250))
                )
        except Exception:
            pass
    return {
        "owned_document_diagnostic": rows,
        "nodes_scanned": len(seen),
        "partial": bool(queue),
        "overview": overview,
    }


def diagnose_owned_field(field):
    from ev.accessibility import AccessibilityBridge

    bridge = AccessibilityBridge()
    node, current = bridge._resolve_node(
        field["application"],
        field["name"],
        field["role"],
        field["process_id"],
        field["window_title"],
    )
    if current["path"] != field["path"]:
        raise RuntimeError("Owned field changed")
    before = {"count": node.get_character_count(), "text": bridge.Atspi.Text.get_text(node, 0, -1)}
    node.clear_cache()
    return {
        "before_clear_cache": before,
        "after_clear_cache": {
            "count": node.get_character_count(),
            "text": bridge.Atspi.Text.get_text(node, 0, -1),
        },
    }


async def tool(name, arguments, *, expected_failure=False):
    result = (await request("tool.call", {"name": name, "arguments": arguments}))["payload"]
    if result.get("status") != ("failed" if expected_failure else "completed"):
        raise RuntimeError(f"{name}: {json.dumps(result)}")
    return result["result"]


async def run(workflow=False, keyboard=False):
    started = time.monotonic()
    snapshot = (await request("snapshot"))["payload"]
    if snapshot["core"]["state"] != "DORMANT" or snapshot["planner"]["active"]:
        raise RuntimeError("E.V. is busy; no browser fixture launched")
    if keyboard and not (await tool("desktop.input.status", {})).get("connected"):
        raise RuntimeError(
            "Keyboard test needs an existing native input grant; no portal was opened"
        )
    binary = shutil.which("firefox-bin") or shutil.which("firefox")
    if not binary:
        raise RuntimeError("Firefox is not installed; no alternative user browser was touched")
    title = "EV Browser Document Fixture " + uuid.uuid4().hex[:8]
    token = uuid.uuid4().hex
    requests, submitted = [], []

    async def page(req):
        requests.append(req.path)
        if req.path.endswith("/form"):
            content = f'<form action="/{token}/finished"><label>Fixture value<input name="value"></label><button>Finish fixture</button></form>'
        elif req.path.endswith("/finished"):
            submitted.append(req.query.get("value"))
            content = "<h1>Received fixture</h1>"
        else:
            content = f'<a href="/{token}/form">Continue to form</a><a href="/{token}/decoy">Unrelated page</a>'
        return web.Response(
            text=f"<!doctype html><title>{title}</title><h1>Owned fixture</h1><p>{token}</p>{content}",
            content_type="text/html",
        )

    app = web.Application()
    app.router.add_get("/" + token, page)
    app.router.add_get("/" + token + "/{step}", page)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/{token}"
    child, window_id = None, None
    previous = (await tool("desktop.world", {})).get("active_window_id")
    try:
        with tempfile.TemporaryDirectory(prefix="ev-browser-document-") as directory:
            shutil.copy2(
                ROOT / "tests/fixtures/browser-profile-user.js", Path(directory) / "user.js"
            )
            child = await asyncio.create_subprocess_exec(
                binary,
                "--no-remote",
                "--profile",
                directory,
                "--new-window",
                url,
                env={**os.environ, "MOZ_ENABLE_WAYLAND": "1"},
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                async with asyncio.timeout(25):
                    while True:
                        world = await tool("desktop.world", {})
                        matches = [
                            w
                            for w in world["windows"]
                            if w.get("pid") == child.pid and title in w.get("title", "")
                        ]
                        if len(matches) == 1:
                            window_id = matches[0]["id"]
                            break
                        if child.returncode is not None:
                            raise RuntimeError(
                                "Owned browser exited before its exact fixture window appeared"
                            )
                        await asyncio.sleep(0.25)
                await tool("desktop.window.activate", {"window_id": window_id})
                condition = {"kind": "browser_url", "window_id": window_id, "expected": url}
                try:
                    ready = await tool(
                        "agent.wait_for",
                        {"conditions": [condition], "timeout_seconds": 12, "interval_seconds": 0.5},
                    )
                    assert ready["verified"], ready
                    observed = await tool("browser.document", {"window_id": window_id})
                except Exception:
                    print(
                        json.dumps(
                            await asyncio.to_thread(
                                diagnose_owned_document, child.pid, matches[0]["title"]
                            )
                        ),
                        flush=True,
                    )
                    raise
                assert observed["url"] == url and observed["busy"] is False, observed
                assert "/" + token in requests  # Independent local server receipt.
                verified = await tool("agent.verify_conditions", {"conditions": [condition]})
                assert verified["verified"], verified
                wrong = await tool(
                    "agent.verify_conditions",
                    {"conditions": [{**condition, "expected": url + "-wrong"}]},
                    expected_failure=True,
                )
                assert not wrong["verified"], wrong
                assert wrong["conditions"][0]["actual"] == url, wrong
                if workflow:

                    async def target(name, role):
                        listing = await tool(
                            "desktop.controls.list",
                            {"window_id": window_id, "query": name, "role": role},
                        )
                        matches = [
                            item
                            for item in listing["items"]
                            if item["name"] == name and item["role"] == role
                        ]
                        if listing.get("truncated") or len(matches) != 1:
                            raise RuntimeError(
                                f"Owned fixture control is missing or ambiguous: {name}"
                            )
                        item = matches[0]
                        assert item["process_id"] == child.pid and title in item["window_title"]
                        diagnostic = await tool(
                            "browser.inspect", {"window_id": window_id, "query": name, "role": role}
                        )
                        print(
                            json.dumps(
                                {
                                    "owned_control": name,
                                    "actions": [
                                        entry.get("actions")
                                        for entry in diagnostic["controls"]
                                        if entry.get("path") == item["id"]
                                    ],
                                }
                            ),
                            flush=True,
                        )
                        resolved = await tool(
                            "desktop.controls.resolve",
                            {"window_id": window_id, "name": name, "role": role},
                        )
                        expected = {
                            key: item[key]
                            for key in (
                                "window_id",
                                "process_id",
                                "window_title",
                                "name",
                                "role",
                                "application",
                            )
                        } | {"path": item["id"]}
                        assert resolved["target"] == expected and resolved["changed"] is False
                        return resolved["target"]

                    async def navigate(control, expected):
                        next_condition = {
                            "kind": "browser_url",
                            "window_id": window_id,
                            "expected": expected,
                        }
                        result = await tool(
                            "agent.execute_plan",
                            {
                                "goal": "Navigate the owned browser fixture and verify its document",
                                "steps": [
                                    {
                                        "id": "activate",
                                        "tool": "desktop.controls.activate",
                                        "arguments": control,
                                    },
                                    {
                                        "id": "wait",
                                        "tool": "agent.wait_for",
                                        "arguments": {
                                            "conditions": [next_condition],
                                            "timeout_seconds": 8,
                                            "interval_seconds": 0.5,
                                        },
                                    },
                                ],
                                "conditions": [next_condition],
                                "timeout_seconds": 15,
                            },
                        )
                        assert result["verified"], result

                    await navigate(await target("Continue to form", "link"), url + "/form")
                    field = await target("Fixture value", "entry")
                    value = "owned " + uuid.uuid4().hex[:12]
                    before = await tool("desktop.controls.inspect", field)
                    assert before.get("text_available") and before.get("text") == "", before
                    try:
                        field_refs = {
                            key: {"$ref": "resolve.result.target." + key} for key in field
                        }
                        text_condition = {
                            "kind": "control_text",
                            "target": {"$ref": "resolve.result.target"},
                            "expected": value,
                        }
                        changed = await tool(
                            "agent.execute_plan",
                            {
                                "goal": "Fill only the exact owned field and verify complete text before submission",
                                "steps": [
                                    {
                                        "id": "resolve",
                                        "tool": "desktop.controls.resolve",
                                        "arguments": {
                                            "window_id": window_id,
                                            "name": "Fixture value",
                                            "role": "entry",
                                        },
                                    },
                                    {
                                        "id": "fill",
                                        "tool": (
                                            "desktop.controls.type_empty"
                                            if keyboard
                                            else "desktop.controls.set_text"
                                        ),
                                        "arguments": {
                                            **field_refs,
                                            "text": value,
                                            **({} if keyboard else {"expected_text": ""}),
                                        },
                                    },
                                    {
                                        "id": "verify",
                                        "tool": "agent.verify_conditions",
                                        "arguments": {"conditions": [text_condition]},
                                    },
                                ],
                                "conditions": [text_condition],
                            },
                        )
                    except Exception:
                        await asyncio.sleep(0.2)
                        later = await tool("desktop.controls.inspect", field)
                        print(
                            json.dumps(
                                {
                                    "owned_field_later": {
                                        key: later.get(key)
                                        for key in ("text", "text_available", "text_truncated")
                                    },
                                    "expected": value,
                                }
                            ),
                            flush=True,
                        )
                        print(
                            json.dumps(await asyncio.to_thread(diagnose_owned_field, field)),
                            flush=True,
                        )
                        raise
                    assert changed["verified"], changed
                    assert not submitted, submitted  # Field filling must not submit.
                    await navigate(
                        await target("Finish fixture", "button"),
                        url + "/finished?" + urlencode({"value": value}),
                    )
                    assert submitted == [
                        value
                    ], submitted  # Independently received once by owned HTTP server.
                    assert not any(path.endswith("/decoy") for path in requests), requests
                print(
                    json.dumps(
                        {
                            "live_browser_document": "PASSED",
                            "exact_url_verified": True,
                            "seconds_including_browser_startup": round(
                                time.monotonic() - started, 3
                            ),
                            "wrong_url_rejected": True,
                            "independent_http_receipt": True,
                            "user_profile_used": False,
                            "model_requests": 0,
                            "page_content_verified": False,
                            "link_form_workflow": "PASSED" if workflow else "NOT_RUN",
                            "keyboard_field_backend": keyboard,
                            "composed_control_identity_refs": workflow,
                            "model_goal_planning_tested": False,
                        }
                    )
                )
            finally:
                # Close only the exact window still bound to our live child.
                try:
                    world = await tool("desktop.world", {})
                    if window_id and any(
                        w.get("id") == window_id and w.get("pid") == child.pid
                        for w in world["windows"]
                    ):
                        if (
                            world.get("active_window_id") == window_id
                            and previous
                            and any(w.get("id") == previous for w in world["windows"])
                        ):
                            await tool("desktop.window.activate", {"window_id": previous})
                        await tool("desktop.window.close", {"window_id": window_id})
                finally:
                    if child.returncode is None:
                        try:
                            child.terminate()
                        except ProcessLookupError:
                            pass
                    await asyncio.wait_for(child.wait(), 5)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument(
        "--workflow",
        action="store_true",
        help="Also navigate a link, fill and submit an owned local form through generic tools/planner",
    )
    parser.add_argument(
        "--keyboard",
        action="store_true",
        help="Use guarded empty-field keyboard entry; requires an existing native input grant",
    )
    args = parser.parse_args()
    if not args.run:
        parser.error("Pass --run to launch a disposable Firefox fixture")
    asyncio.run(run(args.workflow, args.keyboard))
