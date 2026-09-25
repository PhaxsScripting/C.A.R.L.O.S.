"""Silent local comparison: generate replies only, never execute model tools."""

import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from ev.ai.local_llama import LocalHybridProvider, LocalLlamaProvider
from ev.cli import request


async def main():
    baseline_path = Path(sys.argv[1]) / "core/ev/ai/local_llama.py"
    specification = importlib.util.spec_from_file_location(
        "ev.ai.baseline_local_llama", baseline_path
    )
    previous = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = previous
    specification.loader.exec_module(previous)
    config = json.loads(Path.home().joinpath(".config/ev/config.json").read_text())
    catalog = (await request("tool.catalog"))["payload"]["tools"]
    settings = {
        **config["providers"]["local_llama"],
        "temperature": 0,
        "max_output_tokens": 96,
        "casual_max_output_tokens": 64,
    }
    for question in (
        "Is listening to music good while coding?",
        "Pasta sounds nice. What should I try first?",
    ):
        for label, provider_type, router in (
            ("previous", previous.LocalLlamaProvider, previous.LocalHybridProvider),
            ("updated", LocalLlamaProvider, LocalHybridProvider),
        ):
            provider = provider_type(settings, config["personality"])
            if not await provider._healthy():
                raise RuntimeError("Existing local server unavailable; test will not start one")
            selected = router._relevant_tools(question, [], catalog)
            started = time.monotonic()
            result = await provider.begin(question, [], [], selected)
            print(
                json.dumps(
                    {
                        "version": label,
                        "question": question,
                        "tools_sent": len(selected),
                        "model_ms": round((time.monotonic() - started) * 1000),
                        "reply": result.text,
                        "proposed_tools": [call.name for call in result.tool_calls],
                    }
                ),
                flush=True,
            )


asyncio.run(main())
