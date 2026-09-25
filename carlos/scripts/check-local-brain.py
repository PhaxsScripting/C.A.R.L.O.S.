#!/usr/bin/env python3
"""Synthetic, local-only model quality gate. NEVER executes returned actions.

Uses the real provider and actual registered tool schemas, with a private owned
loopback runtime. Does not read cloud credentials, chat, microphone or desktop.
Reports model-only latency; passing samples do not imply end-to-end reliability.
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

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
from ev.ai.local_llama import LocalLlamaProvider
from ev.ai.local_agent import LocalAgentProvider
from ev.ai.base import ProviderResourceError
from ev.config import DEFAULT_CONFIG
from ev.paths import Paths
from ev.service import CarlosCore
from ev.tools.plans import build_plan


async def run(args):
    config = {
        **DEFAULT_CONFIG["providers"]["local_llama"],
        "gpu_layers": args.gpu_layers,
        "prewarm": args.prewarm,
        "max_output_tokens": 512,
        "request_timeout_seconds": 60,
        "structured_decisions": args.structured,
        "incremental_planning": args.structured,
        "compact_decision_prompt": args.compact,
        "search_only_discovery": args.search_only,
        "definitions_at_end": args.definitions_at_end,
        "temperature": 0,
        "sentence_streaming": False,
    }
    if args.threads is not None:
        config.update(threads=args.threads, threads_batch=args.threads)
    for key in ("binary", "model_path"):
        value = getattr(args, key)
        if value:
            config[key] = value
    config["model"] = Path(config["model_path"]).name
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        config["port"] = available.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="ev-local-brain-fixture-") as directory:
        root = Path(directory)
        service = CarlosCore(
            paths=Paths(*(root / n for n in ("config", "data", "state", "cache", "runtime")))
        )
        service.config["security"]["allowed_roots"] = [str(root)]
        provider_type = LocalAgentProvider if args.adapter == "agent" else LocalLlamaProvider
        provider = provider_type(config, ownership_path=root / "owner.json")
        if args.trace:
            original_post = provider._post

            async def traced_post(messages, tools):
                started = time.monotonic()
                print(
                    json.dumps(
                        {
                            "request_tools": [t["name"] for t in tools],
                            "request_characters": len(json.dumps(messages))
                            + len(json.dumps(tools)),
                        }
                    ),
                    flush=True,
                )
                response = await original_post(messages, tools)
                print(
                    json.dumps(
                        {
                            "request_seconds": round(time.monotonic() - started, 3),
                            "usage": response.get("usage", {}),
                            "timings": response.get("timings", {}),
                            "reasoning_characters": len(
                                response.get("choices", [{}])[0]
                                .get("message", {})
                                .get("reasoning_content", "")
                                or ""
                            ),
                            "returned_functions": [
                                c["function"]["name"]
                                for c in response.get("choices", [{}])[0]
                                .get("message", {})
                                .get("tool_calls", [])
                            ],
                        }
                    ),
                    flush=True,
                )
                return response

            provider._post = traced_post
        target = root / "fixture.txt"
        digest = hashlib.sha256(b"fixture").hexdigest()
        cases = [
            (
                "conversation_context",
                "What fruit did I say I liked?",
                [],
                [
                    {"role": "user", "content": "My favorite fruit is a mango."},
                    {"role": "assistant", "content": "Got it, mango."},
                ],
            ),
            (
                "conversation_followup",
                "What could I make with that fruit?",
                [],
                [
                    {"role": "user", "content": "My favorite fruit is a mango."},
                    {"role": "assistant", "content": "Got it, mango."},
                ],
            ),
            (
                "capability_question",
                "Could you help me create a defensive cybersecurity tool? This is a question, not a request to scan anything.",
                ["security.network_exposure"],
                [],
            ),
            (
                "media_request",
                "Unpause my Spotify, bro, what the fuck. Use the media tool with play and player spotify.",
                ["audio.media"],
                [],
            ),
            ("media_natural", "Unpause my Spotify, bro, what the fuck.", ["audio.media"], []),
            (
                "capability_natural",
                "Could you help me create a defensive cybersecurity tool?",
                ["security.network_exposure"],
                [],
            ),
            ("browser_natural", "Open youtube.com in Firefox", ["browser.open_url"], []),
            (
                "create_and_verify",
                f"Return exactly one agent.execute_plan call: create {target} containing exactly fixture without a newline. Use one files.text.create step and a file_hash final condition with sha256 {digest}. This is a model-only test; do not claim you executed it.",
                ["agent.execute_plan", "files.text.create", "files.hash"],
                [],
            ),
        ]
        results = []
        conversation_bridge = None
        try:
            start = time.monotonic()
            await provider._ensure_server()
            print(
                json.dumps(
                    {
                        "model": provider.model,
                        "load_seconds": round(time.monotonic() - start, 3),
                        "cloud_requests": 0,
                        "actions_executed": 0,
                    }
                ),
                flush=True,
            )
            if args.prewarm:
                start = time.monotonic()
                if args.adapter == "agent":
                    await provider.prewarm_with_tools(service.tools.catalog())
                else:
                    await provider.prewarm()
                print(
                    json.dumps(
                        {
                            "prewarm_seconds": round(time.monotonic() - start, 3),
                            "actions_executed": 0,
                        }
                    ),
                    flush=True,
                )
            measured = psutil.Process(provider._process.pid)
            for name, prompt, names, context in cases:
                if args.case and name not in args.case:
                    continue
                if name == "conversation_followup" and conversation_bridge:
                    context = conversation_bridge
                tools = (
                    service.tools.catalog()
                    if args.full_catalog
                    else [service.tools.get(n).public() for n in names]
                )
                start = time.monotonic()
                measured.cpu_percent()
                row = {"case": name, "passed": False}
                resource_stop = False
                try:
                    turn = await provider.begin(prompt, context, [], tools)
                    for call in turn.tool_calls:
                        service.tools.validate(call.name, call.arguments)
                    row.update(
                        text=turn.text,
                        calls=[{"name": c.name, "arguments": c.arguments} for c in turn.tool_calls],
                    )
                    if name in {"conversation_context", "conversation_followup"}:
                        row["passed"] = not turn.tool_calls and "mango" in turn.text.casefold()
                        if name == "conversation_context":
                            conversation_bridge = context + [
                                {"role": "user", "content": prompt},
                                {"role": "assistant", "content": turn.text},
                            ]
                    elif name in {"capability_question", "capability_natural"}:
                        row["passed"] = bool(turn.text.strip()) and not turn.tool_calls
                        row["check_scope"] = (
                            "question_does_not_trigger_scan; answer_quality_unscored"
                        )
                    elif name in {"media_request", "media_natural"}:
                        row["passed"] = (
                            len(turn.tool_calls) == 1
                            and turn.tool_calls[0].name == "audio.media"
                            and turn.tool_calls[0].arguments
                            == {"action": "play", "player": "spotify"}
                        )
                    elif name == "browser_natural":
                        row["passed"] = (
                            len(turn.tool_calls) == 1
                            and turn.tool_calls[0].name == "browser.open_url"
                            and turn.tool_calls[0].arguments.get("url", "").rstrip("/")
                            == "https://youtube.com"
                            and turn.tool_calls[0].arguments.get("browser") == "firefox"
                        )
                        row["check_scope"] = (
                            "exact URL and browser argument selection; execution and page loading unverified"
                        )
                    elif (
                        len(turn.tool_calls) == 1
                        and turn.tool_calls[0].name == "agent.execute_plan"
                    ):
                        plan = build_plan(
                            turn.tool_calls[0].arguments, service.planner, "local-model-only"
                        )
                        row["passed"] = (
                            len(plan.steps) == 1
                            and plan.steps[0].tool == "files.text.create"
                            and plan.steps[0].arguments
                            == {"path": str(target), "content": "fixture"}
                            and plan.goal_conditions
                            == [{"kind": "file_hash", "path": str(target), "sha256": digest}]
                        )
                except Exception as error:
                    row["error"] = f"{type(error).__name__}: {error}"
                    resource_stop = isinstance(error, ProviderResourceError)
                row.update(seconds=round(time.monotonic() - start, 3))
                try:
                    row.update(
                        cpu_percent=measured.cpu_percent(),
                        rss_mib=round(measured.memory_info().rss / 1048576, 1),
                    )
                except psutil.NoSuchProcess:
                    row.update(runtime_exited=True)
                if target.exists():
                    raise RuntimeError("Model-only invariant violated: fixture exists")
                results.append(row)
                print(json.dumps(row), flush=True)
                if resource_stop:
                    print(
                        json.dumps(
                            {"remaining_cases_skipped": "resource guard stopped the owned runtime"}
                        ),
                        flush=True,
                    )
                    break
            print(
                json.dumps(
                    {
                        "passed": sum(r["passed"] for r in results),
                        "total": len(results),
                        "actions_executed": 0,
                        "end_to_end_verified": False,
                    }
                ),
                flush=True,
            )
            return all(r["passed"] for r in results)
        finally:
            await provider.close()
            service.memory.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--binary")
    parser.add_argument("--model-path")
    parser.add_argument("--gpu-layers", type=int, default=0)
    parser.add_argument(
        "--threads",
        type=int,
        choices=range(1, 5),
        help="Bound generation and prompt threads together for thermal/latency comparison",
    )
    parser.add_argument("--adapter", choices=["direct", "agent"], default="direct")
    parser.add_argument(
        "--full-catalog",
        action="store_true",
        help="Also measure the full registered catalog rather than the small scenario catalog",
    )
    parser.add_argument(
        "--trace",
        action="store_true",
        help="Print synthetic request sizes and tool names to locate inference bottlenecks",
    )
    parser.add_argument(
        "--prewarm",
        action="store_true",
        help="Measure the real provider warmup before the synthetic requests",
    )
    parser.add_argument(
        "--structured", action="store_true", help="Benchmark constrained incremental decisions"
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Measure structured protocol without duplicate discovery prose",
    )
    parser.add_argument(
        "--search-only",
        action="store_true",
        help="Discover actual tool definitions without allowing invented load names",
    )
    parser.add_argument(
        "--definitions-at-end",
        action="store_true",
        help="Measure cache-friendly transient catalog suffix",
    )
    parser.add_argument(
        "--case",
        action="append",
        choices=[
            "conversation_context",
            "conversation_followup",
            "capability_question",
            "media_request",
            "create_and_verify",
            "media_natural",
            "capability_natural",
            "browser_natural",
        ],
    )
    args = parser.parse_args()
    if not args.run:
        parser.error("Pass --run to load a private local model and run synthetic checks")
    sys.exit(0 if asyncio.run(run(args)) else 1)
