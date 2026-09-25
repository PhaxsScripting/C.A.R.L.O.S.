#!/usr/bin/env python3
"""Installed background watcher check using only a disposable owned child.

Creates one bounded persistent E.V. watch receipt; --restart-core additionally
restarts only an idle E.V. core. No model request, GUI launch or signal to an
existing application. No task-success claim follows from the child's lifetime.
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from ev.cli import request


async def tool(name, arguments=None):
    result = (await request("tool.call", {"name": name, "arguments": arguments or {}}))["payload"]
    if result.get("status") != "completed":
        raise RuntimeError(f"Fixture tool {name} did not complete")
    return result["result"]


async def run(restart_core=False):
    snapshot = (await request("snapshot"))["payload"]
    if snapshot["core"]["state"] != "DORMANT" or snapshot["planner"]["active"]:
        raise RuntimeError("E.V. is busy; fixture was not started")
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)", stdin=asyncio.subprocess.PIPE
    )
    started, watch_id, ended = time.monotonic(), None, False
    try:
        observation = await tool("system.process_lifetime", {"pid": child.pid})
        identity = {k: observation[k] for k in ("pid", "start_ticks", "boot_id")}
        registration = await tool("agent.watch_process", {**identity, "timeout_seconds": 90})
        watch_id = registration["watch"]["id"]
        if restart_core:
            fresh = (await request("snapshot"))["payload"]
            if (
                fresh["pid"] != snapshot["pid"]
                or fresh["core"]["state"] != "DORMANT"
                or fresh["planner"]["active"]
            ):
                raise RuntimeError("Core changed or became busy; restart was not attempted")
            await request("core.stop")
            async with asyncio.timeout(45):
                while True:
                    activation = await asyncio.create_subprocess_exec(
                        str(Path.home() / ".local/bin/ev-activate"),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await asyncio.wait_for(activation.wait(), 35)
                    try:
                        fresh = (await asyncio.wait_for(request("snapshot"), 2))["payload"]
                        if fresh["pid"] != snapshot["pid"]:
                            break
                    except (OSError, RuntimeError, asyncio.TimeoutError):
                        pass
                    await asyncio.sleep(0.2)
            watched = next(
                item
                for item in (await tool("agent.process_watches"))["watches"]
                if item.get("id") == watch_id
            )
            assert watched["status"] == "WAITING" and watched["identity"] == identity
            assert child.returncode is None
        else:
            await asyncio.sleep(2)
        child.stdin.write(b"x")
        await child.stdin.drain()
        child.stdin.close()
        await child.wait()
        async with asyncio.timeout(8):
            while True:
                watches = (await tool("agent.process_watches"))["watches"]
                watched = next(item for item in watches if item.get("id") == watch_id)
                if watched["status"] != "WAITING":
                    break
                await asyncio.sleep(0.25)
        assert watched["status"] == "LIFETIME_ENDED", watched["status"]
        assert watched["identity"] == identity
        assert watched["task_success_verified"] is False
        assert watched["actions_replayed"] is False
        assert watched["exit_code_known"] is False
        ended = True
        print(
            json.dumps(
                {
                    "live_background_watch": "PASSED",
                    "seconds": round(time.monotonic() - started, 3),
                    "lifetime_ended": True,
                    "job_success_verified": False,
                    "actions_replayed": False,
                    "cloud_requests": 0,
                    "desktop_mutations": 0,
                    "fixture_receipts_retained": 1,
                    "real_core_restart_verified": restart_core,
                }
            )
        )
    finally:
        child.stdin.close()
        await asyncio.wait_for(child.wait(), 3)
        if watch_id and not ended:
            await tool("agent.cancel_process_watch", {"id": watch_id})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument(
        "--restart-core",
        action="store_true",
        help="Gracefully restart only the idle E.V. core while the disposable child is held alive",
    )
    args = parser.parse_args()
    if not args.run:
        parser.error("Pass --run to create a disposable process and watch receipt")
    asyncio.run(run(args.restart_core))
