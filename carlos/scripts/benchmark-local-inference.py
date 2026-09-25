#!/usr/bin/env python3
"""Compare llama.cpp CPU profiles without changing the running E.V. service.

Run with PYTHONPATH=core python3 scripts/benchmark-local-inference.py.
Each profile owns a temporary loopback server and reports wall time, aggregate
CPU consumption (100% = one logical CPU), and server token timings as JSON.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import time

import aiohttp
import psutil

from ev.ai.local_llama import LocalLlamaProvider
from ev.config import DEFAULT_CONFIG


async def benchmark(threads: int, poll: int, repeats: int, overrides: dict | None = None) -> None:
    config = {**DEFAULT_CONFIG["providers"]["local_llama"], **(overrides or {})}
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    command = [
        "/usr/bin/nice",
        "-n",
        str(config["nice"]),
        config["binary"],
        "--model",
        config["model_path"],
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--ctx-size",
        str(config["context_size"]),
        "--threads",
        str(threads),
        "--threads-batch",
        str(threads),
        "--parallel",
        "1",
        "--gpu-layers",
        str(config["gpu_layers"]),
        "--poll",
        str(poll),
        "--poll-batch",
        "1" if poll else "0",
        "--threads-http",
        "2",
        "--jinja",
        "--no-webui",
        "--log-disable",
    ]
    if config.get("op_offload") is False:
        command.append("--no-op-offload")
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
            deadline = time.monotonic() + 45
            while True:
                if process.returncode is not None:
                    raise RuntimeError(f"Benchmark server exited: {process.returncode}")
                try:
                    async with session.get(base_url + "/health") as response:
                        if response.status == 200:
                            break
                except aiohttp.ClientError:
                    pass
                if time.monotonic() >= deadline:
                    raise TimeoutError("Benchmark server loading timed out")
                await asyncio.sleep(0.25)
            measured = psutil.Process(process.pid)
            payload = {
                "messages": [
                    {
                        "role": "system",
                        "content": LocalLlamaProvider(config)._system_instructions(),
                    },
                    {
                        "role": "user",
                        "content": "Briefly explain why a computer uses CPU to run a local language model.",
                    },
                ],
                "temperature": 0,
                "seed": 7,
                "max_tokens": 48,
                "cache_prompt": True,
                "stream": False,
                "chat_template_kwargs": {"enable_thinking": False},
            }
            for attempt in range(repeats):
                measured.cpu_percent()
                started = time.perf_counter()
                async with session.post(
                    base_url + "/v1/chat/completions", json=payload
                ) as response:
                    response.raise_for_status()
                    result = await response.json()
                print(
                    json.dumps(
                        {
                            "threads": threads,
                            "poll": poll,
                            "attempt": attempt,
                            "binary": config["binary"],
                            "model_path": config["model_path"],
                            "gpu_layers": config["gpu_layers"],
                            "op_offload": config.get("op_offload", "runtime_default"),
                            "seconds": round(time.perf_counter() - started, 3),
                            "cpu_percent": measured.cpu_percent(),
                            "rss_mib": round(measured.memory_info().rss / 1048576, 1),
                            "response": result.get("choices", [{}])[0]
                            .get("message", {})
                            .get("content"),
                            "timings": result.get("timings"),
                            "usage": result.get("usage"),
                        }
                    ),
                    flush=True,
                )
            measured.cpu_percent()
            await asyncio.sleep(2)
            print(
                json.dumps(
                    {"threads": threads, "poll": poll, "idle_cpu_percent": measured.cpu_percent()}
                ),
                flush=True,
            )
    finally:
        if process.returncode is None:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threads", type=int, nargs="+", default=[4, 3, 2])
    parser.add_argument("--poll", type=int, nargs="+", default=[50, 0])
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--binary")
    parser.add_argument("--model-path")
    parser.add_argument("--gpu-layers", type=int, default=0)
    parser.add_argument("--disable-op-offload", action="store_true")
    args = parser.parse_args()
    if (
        args.repeats < 1
        or any(n < 1 for n in args.threads)
        or any(n < 0 or n > 100 for n in args.poll)
    ):
        parser.error("threads/repeats must be positive and poll must be between 0 and 100")
    for threads in args.threads:
        for poll in args.poll:
            overrides = {"gpu_layers": args.gpu_layers}
            if args.disable_op_offload:
                overrides["op_offload"] = False
            if args.binary:
                overrides["binary"] = args.binary
            if args.model_path:
                overrides["model_path"] = args.model_path
            await benchmark(threads, poll, args.repeats, overrides)


if __name__ == "__main__":
    asyncio.run(main())
