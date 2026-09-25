#!/usr/bin/env python3
"""Opt-in synthetic NVIDIA plan check. Returned tools are NEVER executed.

Sends only a synthetic fixture request and tool definitions; no chat, microphone,
screen, saved memories or project source. Uses the configured account's API quota.
"""

import argparse
import asyncio
import hashlib
import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
from ev.ai.nvidia import NvidiaProvider
from ev.config import load_config
from ev.paths import Paths, get_paths
from ev.service import CarlosCore
from ev.tools.plans import build_plan


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument(
        "--conversation",
        action="store_true",
        help="Test ordinary synthetic conversation through begin(), not plan generation",
    )
    args = parser.parse_args()
    if not args.run:
        parser.error("Pass --run to send the synthetic model-only API request")
    config = load_config(get_paths())
    if config["providers"]["active"] != "nvidia":
        raise RuntimeError("NVIDIA is not the active provider; not selecting another account")
    credential = get_paths().config_dir / "provider.env"
    fd = os.open(credential, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size > 16384
        ):
            raise RuntimeError("Provider credential file is not private or bounded")
        for line in handle.read(16384).splitlines():
            key, sep, value = line.partition("=")
            if sep and key == "NVIDIA_API_KEY":
                os.environ[key] = value
    provider = NvidiaProvider(
        {**config["providers"]["nvidia"], "max_output_tokens": 1600, "turn_timeout_seconds": 25}
    )
    with tempfile.TemporaryDirectory(prefix="ev-cloud-schema-fixture-") as directory:
        root = Path(directory)
        service = CarlosCore(
            paths=Paths(*(root / n for n in ("config", "data", "state", "cache", "runtime")))
        )
        service.config["security"]["allowed_roots"] = [str(root)]
        target = root / "fixture.txt"
        digest = hashlib.sha256(b"fixture").hexdigest()
        names = ["agent.execute_plan", "files.text.create", "files.hash"]
        catalog = [service.tools.get(n).public() for n in names]
        prompt = f"This is a model-only schema test. Return exactly one agent.execute_plan tool call (the harness will validate it but never execute anything). The hypothetical task is: create {target} containing exactly fixture without a newline, then verify its SHA-256 is {digest}. Use files.text.create inside the plan and a file_hash condition. Do not claim execution."
        started = time.monotonic()
        try:
            if args.conversation:
                turn = await provider.begin(
                    "What does HTTP mean? Answer in one short sentence.",
                    [],
                    [],
                    service.tools.catalog(),
                )
                if turn.tool_calls or not turn.text.strip():
                    raise RuntimeError(
                        "Conversation did not produce a plain answer; no tools executed"
                    )
                print(
                    json.dumps(
                        {
                            "provider": "nvidia",
                            "conversation": "LIVE_ACCEPTED",
                            "response": turn.text,
                            "actions_executed": 0,
                            "private_context_sent": False,
                            "latency_ms": round((time.monotonic() - started) * 1000, 1),
                        }
                    )
                )
                return
            turn = await provider._respond(
                [{"role": "user", "content": prompt}], catalog, names, started
            )
            if len(turn.tool_calls) != 1 or turn.tool_calls[0].name != "agent.execute_plan":
                raise RuntimeError(
                    "Model did not return the requested composite tool; no actions executed"
                )
            call = turn.tool_calls[0]
            print(
                json.dumps(
                    {
                        "synthetic_argument_types": {
                            k: type(v).__name__ for k, v in call.arguments.items()
                        }
                    }
                ),
                flush=True,
            )
            service.tools.validate(call.name, call.arguments)
            plan = build_plan(call.arguments, service.planner, "model-only")
            if (
                len(plan.steps) != 1
                or plan.steps[0].tool != "files.text.create"
                or plan.steps[0].arguments != {"path": str(target), "content": "fixture"}
            ):
                raise RuntimeError("Returned plan does not match the exact synthetic action")
            if plan.goal_conditions != [
                {"kind": "file_hash", "path": str(target), "sha256": digest}
            ]:
                raise RuntimeError("Returned plan does not match the synthetic final condition")
            if target.exists():
                raise RuntimeError("Model-only invariant failed: fixture unexpectedly exists")
            print(
                json.dumps(
                    {
                        "provider": "nvidia",
                        "model": provider.model,
                        "wire_schema": "LIVE_ACCEPTED",
                        "returned_plan": "LOCALLY_VALIDATED",
                        "actions_executed": 0,
                        "private_context_sent": False,
                        "latency_ms": round((time.monotonic() - started) * 1000, 1),
                    }
                )
            )
        finally:
            await provider.close()
            service.memory.close()


if __name__ == "__main__":
    asyncio.run(main())
