#!/usr/bin/env python3
"""Silent CPU comparison with the production ownership, RAM and thermal guards."""

import argparse
import asyncio
import json
import socket
import tempfile
import time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from ev.ai.local_llama import LocalLlamaProvider
from ev.ai.streaming import observer
from ev.telemetry import read_temperature


async def measure(threads, repeats):
    full = json.loads((Path.home() / ".config/ev/config.json").read_text())
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    settings = {
        **full["providers"]["local_llama"],
        "port": port,
        "threads": threads,
        "threads_batch": threads,
        "poll": 0,
        "max_output_tokens": 64,
        "casual_max_output_tokens": 48,
        "temperature": 0,
    }
    with tempfile.TemporaryDirectory(prefix="carlos-model-benchmark-") as temp:
        model = LocalLlamaProvider(settings, full.get("personality", {}), Path(temp) / "owner.json")
        try:
            for attempt in range(repeats):
                sample = {
                    "threads": threads,
                    "attempt": attempt,
                    "temperature_before": read_temperature(),
                }

                def seen(kind, text, latency_ms):
                    if kind == "first_token":
                        sample["first_token_ms"] = round(latency_ms, 2)

                token = observer.set(seen)
                start = time.monotonic()
                try:
                    turn = await asyncio.wait_for(
                        model.begin("In one sentence, what does RAM do?", [], [], []), 45
                    )
                    sample.update(status="completed", response=turn.text, usage=turn.usage)
                except Exception as error:
                    sample.update(status="failed", error=str(error))
                finally:
                    observer.reset(token)
                sample.update(
                    seconds=round(time.monotonic() - start, 3), temperature_after=read_temperature()
                )
                print(json.dumps(sample), flush=True)
                if sample["status"] != "completed":
                    break
        finally:
            await model.close()


async def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--threads", type=int, nargs="+", default=[1, 2])
    p.add_argument("--repeats", type=int, default=2)
    args = p.parse_args()
    if not 1 <= args.repeats <= 5 or any(not 1 <= n <= 3 for n in args.threads):
        p.error("Use 1-3 threads and 1-5 repeats")
    for count in args.threads:
        await measure(count, args.repeats)


if __name__ == "__main__":
    asyncio.run(main())
