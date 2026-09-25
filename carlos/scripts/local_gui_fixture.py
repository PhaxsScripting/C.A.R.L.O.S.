"""Local model -> existing planner -> installed generic GUI tools, fixture only.

This is an opt-in benchmark, not a production execution adapter. It denies all
non-fixture windows, cloud calls, files, shell, keyboard and arbitrary buttons.
"""

import asyncio
import json
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
from ev.ai.local_agent import LocalAgentProvider
from ev.config import DEFAULT_CONFIG
from ev.paths import Paths
from ev.service import CarlosCore


async def run_local_picker(model_path, window_id, pid, title, fixture_file, threads=1):
    executable = str(Path.home() / ".local/bin/evctl")

    def ipc(name, arguments):
        result = subprocess.run(
            [executable, "tool", name, "--arguments", json.dumps(arguments)],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return json.loads(result.stdout)["payload"]

    allowed = {
        "desktop.world",
        "desktop.window.activate",
        "desktop.controls.list",
        "desktop.controls.inspect",
        "desktop.controls.activate",
        "desktop.controls.set_state",
    }
    with tempfile.TemporaryDirectory(prefix="ev-local-gui-task-") as directory:
        root = Path(directory)
        service = CarlosCore(
            paths=Paths(*(root / n for n in ("config", "data", "state", "cache", "runtime")))
        )
        config = {
            **DEFAULT_CONFIG["providers"]["local_llama"],
            "model_path": str(model_path),
            "model": Path(model_path).name,
            "prewarm": False,
            "gpu_layers": 0,
            "max_output_tokens": 512,
            "sentence_streaming": False,
            "structured_decisions": True,
            "incremental_planning": True,
            "compact_decision_prompt": True,
            "turn_timeout_seconds": 60,
            "search_only_discovery": True,
        }
        config.update(threads=threads, threads_batch=threads)
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            config["port"] = available.getsockname()[1]
        provider = LocalAgentProvider(config, ownership_path=root / "owner.json")
        original_post = provider._post

        async def trace(messages, tools):
            result = await original_post(messages, tools)
            print(
                json.dumps(
                    {
                        "gui_model_decision": result.get("choices"),
                        "tools": [t["name"] for t in tools],
                    }
                ),
                flush=True,
            )
            return result

        provider._post = trace
        service.brain.provider = provider
        service.brain.tools = [service.tools.get(name).public() for name in sorted(allowed)]
        blocked, invoked = [], []

        async def guarded(payload, correlation_id=None, **kwargs):
            name, args = payload.get("name"), payload.get("arguments", {})
            # Fresh window ownership before EVERY request. No model-provided
            # identity can expand scope to another desktop application.
            observation = await asyncio.to_thread(ipc, "desktop.world", {})
            world = observation.get("result", {})
            owned = [
                w
                for w in world.get("windows", [])
                if w.get("pid") == pid
                and w.get("title") in {title, "E.V. disposable file chooser" + title}
            ]
            permitted = name in allowed and (
                name == "desktop.world" or args.get("window_id") in {w["id"] for w in owned}
            )
            if name == "desktop.controls.activate":
                permitted = permitted and args.get("name") in {"Choose fixture file", "Open"}
            if name == "desktop.controls.set_state":
                permitted = (
                    permitted
                    and args.get("name") == fixture_file.name
                    and args.get("state") == "selected"
                    and args.get("value") is True
                )
            if not permitted or observation.get("status") != "completed":
                blocked.append(name)
                return {
                    "status": "failed",
                    "tool": name,
                    "error": "Fixture guard rejected request; no action dispatched",
                }
            if name == "desktop.world":
                result = {
                    **observation,
                    "result": {
                        **world,
                        "windows": owned,
                        "active_window_id": (
                            world.get("active_window_id")
                            if world.get("active_window_id") in {w["id"] for w in owned}
                            else ""
                        ),
                        "scope": "owned disposable fixture only",
                    },
                }
            else:
                result = await asyncio.to_thread(ipc, name, args)
            invoked.append(name)
            print(
                json.dumps({"gui_model_tool": name, "arguments": args, "result": result}),
                flush=True,
            )
            return result

        service.request_tool = guarded
        prompt = (
            f"In the already open disposable application window {window_id}, titled {title!r}, "
            f"choose the file named {fixture_file.name!r} through its file chooser and confirm Open. "
            "The file is already in the chooser's current folder. Inspect the interface to find the necessary controls. "
            "Do not modify or launch any other application. Do not claim success without observing the result."
        )
        start = time.monotonic()
        try:
            async with asyncio.timeout(180):
                await provider._ensure_server()
                result = await service._submit_action_clauses(prompt, "local-gui-fixture")
            print(
                json.dumps(
                    {
                        "local_gui_result": {
                            "status": result.get("status"),
                            "response": result.get("response"),
                            "execution_status": result.get("execution_status"),
                            "seconds": round(time.monotonic() - start, 3),
                            "invoked": invoked,
                            "blocked": blocked,
                            "cloud_requests": 0,
                            "goal_verification": "parent harness must independently observe chooser callback",
                        }
                    }
                ),
                flush=True,
            )
            print(
                json.dumps(
                    {
                        "model_errors": [
                            e["payload"]
                            for e in service.bus.history()
                            if e["type"] in {"ai.decision_rejected", "ai.request_failed"}
                        ]
                    }
                ),
                flush=True,
            )
            return result.get("status") == "completed" and not blocked
        finally:
            await provider.close()
            service.memory.close()
