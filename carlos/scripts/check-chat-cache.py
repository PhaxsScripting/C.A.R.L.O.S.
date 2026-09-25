#!/usr/bin/env python3
"""Silent, no-tool cache-shift benchmark against E.V.'s existing local server."""

import asyncio
import json
import sys
import time
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from ev.ai.local_llama import LocalLlamaProvider, CASUAL_STYLE


async def main():
    config = json.loads(Path.home().joinpath(".config/ev/config.json").read_text())
    provider = LocalLlamaProvider(config["providers"]["local_llama"], config["personality"])
    history = [
        {"role": "user", "content": "I'd like to learn to cook simple meals." + CASUAL_STYLE},
        {"role": "assistant", "content": "We can start with an easy pasta dish or a sandwich."},
        {
            "role": "user",
            "content": "Pasta sounds nice. I have tomatoes and garlic." + CASUAL_STYLE,
        },
        {
            "role": "assistant",
            "content": "Tomatoes and garlic are a good start for a simple sauce.",
        },
        {"role": "user", "content": "I also have some dried basil. Does that work?" + CASUAL_STYLE},
        {"role": "assistant", "content": "Yes, dried basil goes well with tomatoes and garlic."},
        {"role": "user", "content": "What about adding cheese at the end?" + CASUAL_STYLE},
        {"role": "assistant", "content": "A little grated cheese at the end would work well."},
    ]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        for reuse in (0, 64):
            for offset in (0, 2):
                messages = (
                    [{"role": "system", "content": provider._system_instructions()}]
                    + history[offset:]
                    + [
                        {
                            "role": "user",
                            "content": "What ingredients did I say I have? Give one short sentence."
                            + CASUAL_STYLE,
                        }
                    ]
                )
                payload = {
                    "messages": messages,
                    "max_tokens": 40,
                    "temperature": 0,
                    "stream": False,
                    "cache_prompt": True,
                    "n_cache_reuse": reuse,
                }
                started = time.monotonic()
                async with session.post(
                    provider.base_url + "/v1/chat/completions", json=payload
                ) as response:
                    response.raise_for_status()
                    result = await response.json()
                print(
                    json.dumps(
                        {
                            "reuse": reuse,
                            "offset": offset,
                            "seconds": round(time.monotonic() - started, 2),
                            "timings": result.get("timings"),
                            "usage": result.get("usage"),
                            "reply": result["choices"][0]["message"]["content"],
                        }
                    ),
                    flush=True,
                )


asyncio.run(main())
