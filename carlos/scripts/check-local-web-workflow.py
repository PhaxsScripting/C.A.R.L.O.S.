#!/usr/bin/env python3
"""Real local model + planner + web parser against synthetic two-page transport.

No public requests, browser profile, credentials or desktop actions. Only the
private owned llama endpoint is contacted. This is not live-website parity.
"""

import argparse
import asyncio
import json
import secrets
import socket
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
from ev.ai.local_agent import LocalAgentProvider
from ev.config import DEFAULT_CONFIG
from ev.paths import Paths
from ev.service import CarlosCore


async def run(args):
    with tempfile.TemporaryDirectory(prefix="ev-local-web-fixture-") as directory:
        root = Path(directory)
        service = CarlosCore(
            paths=Paths(*(root / name for name in ("config", "data", "state", "cache", "runtime")))
        )
        config = {
            **DEFAULT_CONFIG["providers"]["local_llama"],
            "model_path": args.model_path,
            "model": Path(args.model_path).name,
            "prewarm": False,
            "gpu_layers": 0,
            "threads": args.threads,
            "threads_batch": args.threads,
            "max_output_tokens": 512,
            "temperature": 0,
            "sentence_streaming": False,
            "structured_decisions": True,
            "incremental_planning": True,
            "compact_decision_prompt": True,
            "search_only_discovery": True,
        }
        with socket.socket() as endpoint:
            endpoint.bind(("127.0.0.1", 0))
            config["port"] = endpoint.getsockname()[1]
        provider = LocalAgentProvider(config, ownership_path=root / "owner.json")
        service.brain.provider = provider
        nonce = secrets.token_hex(4)
        start = "https://example.net/fixture/" + nonce
        target = "https://example.net/manual/" + secrets.token_hex(4)
        decoy = "https://example.net/archive/" + secrets.token_hex(4)
        answer = "EV-" + secrets.token_hex(4).upper()
        pages = {
            start: f'<p>Documentation index</p><a href="{decoy}">Older manual</a><a href="{target}">Current calibration guide</a>',
            target: f"<h1>Current calibration guide</h1><p>The calibration code is {answer}.</p>",
            decoy: "<h1>Older manual</h1><p>This obsolete page contains no current code.</p>",
        }
        fetched, blocked = [], []

        async def download(url):
            if url not in pages:
                raise ValueError("Fixture URL was not observed; no network request was sent")
            fetched.append(url)
            return url, pages[url]

        original_request = service.request_tool

        async def request(payload, correlation_id=None, **kwargs):
            if (
                payload.get("name") != "web.fetch"
                or payload.get("arguments", {}).get("url") not in pages
            ):
                blocked.append(payload.get("name"))
                return {
                    "status": "failed",
                    "error": "Fixture permits only the three synthetic pages; no action executed",
                }
            result = await original_request(payload, correlation_id, **kwargs)
            print(
                json.dumps(
                    {
                        "event": "fetch",
                        "arguments": payload["arguments"],
                        "status": result.get("status"),
                    }
                ),
                flush=True,
            )
            return result

        service.request_tool = request
        began = time.monotonic()
        try:
            # Actual registry, provider discovery, validation, planner, parser,
            # receipts and continuation; only the HTTP page transport is fake.
            with patch("ev.web_lookup.download", new=AsyncMock(side_effect=download)):
                async with asyncio.timeout(180):
                    result = await service._submit_action_clauses(
                        f"Read {start}, follow its Current calibration guide link, and tell me the calibration code from that guide.",
                        "local-web-fixture",
                    )
            passed = (
                result.get("status") == "completed"
                and answer in str(result.get("response", ""))
                and start in fetched
                and target in fetched
                and fetched.index(start) < fetched.index(target)
                and not blocked
            )
            print(
                json.dumps(
                    {
                        "passed": passed,
                        "seconds": round(time.monotonic() - began, 3),
                        "status": result.get("status"),
                        "response": result.get("response"),
                        "fetched": fetched,
                        "blocked_tools": blocked,
                        "expected_code": answer,
                        "public_requests": 0,
                        "desktop_actions": 0,
                        "live_website_parity": False,
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
                        "error_type": type(error).__name__,
                        "error": str(error),
                        "fetched": fetched,
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
    parser.add_argument("--threads", type=int, choices=range(1, 5), default=1)
    args = parser.parse_args()
    if not args.run:
        parser.error("Pass --run to load an isolated local model")
    sys.exit(0 if asyncio.run(run(args)) else 1)
