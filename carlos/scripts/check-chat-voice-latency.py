#!/usr/bin/env python3
"""Silent local synthetic checks: no microphone, playback, or action tools."""

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from ev.ai.local_llama import LocalLlamaProvider
from ev.voice.tts import TtsRouter


async def main():
    config = json.loads(Path.home().joinpath(".config/ev/config.json").read_text())
    provider = LocalLlamaProvider(config["providers"]["local_llama"], config["personality"])
    if not await provider._healthy():
        raise RuntimeError("Start E.V. first; this test must not spawn another model server.")
    tts = TtsRouter(config["voice"]["tts"])
    history = []
    try:
        for question in (
            "I'd like to cook something simple with tomatoes and garlic. Any ideas?",
            "Pasta sounds good. I also have dried basil.",
            "Can I add cheese at the end?",
            "How do I keep the garlic from burning?",
            "What heat should I use?",
            "Remind me which herb I said I have?",
        ):
            started = time.monotonic()
            turn = await provider.begin(question, history, [], [])
            model_ms = (time.monotonic() - started) * 1000
            started = time.monotonic()
            audio = await tts.synthesize(turn.text)
            voice_ms = (time.monotonic() - started) * 1000
            print(
                json.dumps(
                    {
                        "question": question,
                        "answer": turn.text,
                        "model_ms": round(model_ms),
                        "voice_ready_ms": round(voice_ms),
                        "audio_seconds": round(
                            len(audio.pcm) / (audio.sample_rate * audio.sample_width), 2
                        ),
                        "engine": audio.engine,
                        "worker_pid": tts.piper._worker.pid if tts.piper._worker else None,
                        "tool_calls": len(turn.tool_calls),
                    }
                ),
                flush=True,
            )
            history.extend(
                [{"role": "user", "content": question}, {"role": "assistant", "content": turn.text}]
            )
    finally:
        await tts.close()


asyncio.run(main())
