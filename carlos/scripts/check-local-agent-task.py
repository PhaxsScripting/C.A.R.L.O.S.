#!/usr/bin/env python3
"""Real local-model -> planner -> executor test, confined to a disposable folder.

No desktop input, application control, shell execution, cloud requests, or live
configuration changes are allowed. Real file tools operate only inside the
temporary workspace; each leaf call is checked even inside a composite plan.
"""

import argparse
import asyncio
import hashlib
import json
import socket
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


async def run(args):
    with tempfile.TemporaryDirectory(prefix="ev-local-agent-task-") as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        paths = Paths(*(root / n for n in ("config", "data", "state", "cache", "runtime")))
        service = CarlosCore(paths=paths)
        config = {
            **DEFAULT_CONFIG["providers"]["local_llama"],
            "model_path": args.model_path,
            "model": Path(args.model_path).name,
            "prewarm": False,
            "gpu_layers": 0,
            "max_output_tokens": 512,
            "temperature": 0,
            "sentence_streaming": False,
            "turn_timeout_seconds": args.turn_timeout,
            "structured_decisions": args.structured,
            "incremental_planning": args.structured,
        }
        config["compact_decision_prompt"] = args.compact
        config["search_only_discovery"] = args.search_only
        config["definitions_at_end"] = args.definitions_at_end
        if args.threads is not None:
            config.update(threads=args.threads, threads_batch=args.threads)
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            config["port"] = available.getsockname()[1]
        provider = LocalAgentProvider(config, ownership_path=root / "owner.json")
        original_post = provider._post

        async def traced_post(messages, tools):
            started = time.monotonic()
            response = await original_post(messages, tools)
            print(
                json.dumps(
                    {
                        "event": "model_turn",
                        "seconds": round(time.monotonic() - started, 3),
                        "exposed": [t["name"] for t in tools],
                        "usage": response.get("usage"),
                        "choices": response.get("choices"),
                    },
                    default=str,
                ),
                flush=True,
            )
            return response

        provider._post = traced_post
        service.brain.provider = provider
        service.config["security"]["allowed_roots"] = [str(workspace)]
        original_request = service.request_tool
        invoked, blocked = [], []
        leaves = {
            "files.directory.create",
            "files.text.create",
            "files.hash",
            "files.list",
            "files.info",
            "files.read",
        }
        composites = {
            "agent.execute_plan",
            "agent.verify_conditions",
            "agent.history",
            "agent.task_status",
        }

        async def guarded_request(payload, correlation_id=None, **kwargs):
            name = payload.get("name")
            arguments = payload.get("arguments", {})
            allowed = name in composites
            if name in leaves:
                path = arguments.get("path")
                allowed = (
                    isinstance(path, str)
                    and Path(path).is_absolute()
                    and Path(path).resolve().is_relative_to(workspace)
                )
            if not allowed:
                blocked.append(name)
                return {
                    "status": "failed",
                    "tool": name,
                    "error": "Isolated benchmark permits only file operations within its explicit workspace. No action was executed.",
                }
            invoked.append(name)
            result = await original_request(payload, correlation_id, **kwargs)
            # Fixture-only evidence: no live user transcript, account or files.
            # Keep rejected plan arguments so validation failures are actionable.
            print(
                json.dumps(
                    {
                        "event": "tool_result",
                        "tool": name,
                        "arguments": arguments,
                        "result": result,
                    },
                    default=str,
                ),
                flush=True,
            )
            return result

        service.request_tool = guarded_request
        prompt = f"Inside {workspace}, make a folder named work, and inside that folder create note.txt containing exactly hello, with no newline. Check the file contents afterward."
        started = time.monotonic()
        try:
            async with asyncio.timeout(180):
                await provider._ensure_server()
                result = await service._submit_action_clauses(prompt, "local-files-fixture")
            target = workspace / "work/note.txt"
            contents = target.read_bytes() if target.is_file() else None
            passed = contents == b"hello" and result.get("status") == "completed" and not blocked
            print(
                json.dumps(
                    {
                        "passed": passed,
                        "model": provider.model,
                        "seconds": round(time.monotonic() - started, 3),
                        "status": result.get("status"),
                        "execution_status": result.get("execution_status"),
                        "response": result.get("response"),
                        "tools_invoked": invoked,
                        "blocked_tools": blocked,
                        "file_sha256": (
                            hashlib.sha256(contents).hexdigest() if contents is not None else None
                        ),
                        "expected_contents_verified": contents == b"hello",
                        "cloud_requests": 0,
                        "desktop_actions": 0,
                        "scope": "disposable filesystem fixture; not general desktop parity",
                    }
                ),
                flush=True,
            )
            return passed
        except Exception as error:
            print(
                json.dumps(
                    {
                        "passed": False,
                        "error": str(error),
                        "error_type": type(error).__name__,
                        "seconds": round(time.monotonic() - started, 3),
                        "tools_invoked": invoked,
                        "blocked_tools": blocked,
                        "cloud_requests": 0,
                        "desktop_actions": 0,
                    }
                ),
                flush=True,
            )
            return False
        finally:
            await provider.close()
            service.memory.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--turn-timeout", type=int, choices=range(1, 121), default=60)
    parser.add_argument(
        "--threads", type=int, choices=range(1, 5), help="Bound prompt/generation CPU together"
    )
    parser.add_argument(
        "--structured",
        action="store_true",
        help="Test grammar-constrained incremental local decisions",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Remove duplicate structured-protocol prose while retaining policy",
    )
    parser.add_argument(
        "--search-only",
        action="store_true",
        help="Use registry search instead of exact-name loading",
    )
    parser.add_argument(
        "--definitions-at-end",
        action="store_true",
        help="Measure cache-friendly transient catalog suffix",
    )
    args = parser.parse_args()
    if not args.run:
        parser.error("Pass --run to allow real writes inside the disposable test workspace")
    sys.exit(0 if asyncio.run(run(args)) else 1)
