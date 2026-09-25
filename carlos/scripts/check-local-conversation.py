#!/usr/bin/env python3
"""Silent model-only checks against an already-running loopback llama server."""

import asyncio
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from ev.ai import LocalHybridProvider
from ev.config import DEFAULT_CONFIG


async def main():
    provider = LocalHybridProvider(copy.deepcopy(DEFAULT_CONFIG["providers"]["local_llama"]))
    if not await provider.local._healthy():
        raise RuntimeError("Start E.V. first. This check will not spawn another model server.")
    history = []
    for question in (
        "Can you help me create cyber security tools for my own lab?",
        "I want to learn Python first, but I'm a beginner.",
        "What would a good first project be?",
        "No, I meant something smaller. I only know variables and loops.",
    ):
        result = await provider.begin(question, history, [], [])
        print(
            json.dumps(
                {
                    "question": question,
                    "answer": result.text,
                    "latency_ms": round(result.latency_ms),
                    "tools": [c.name for c in result.tool_calls],
                    "usage": result.usage,
                }
            ),
            flush=True,
        )
        history.extend(
            [{"role": "user", "content": question}, {"role": "assistant", "content": result.text}]
        )


asyncio.run(main())
