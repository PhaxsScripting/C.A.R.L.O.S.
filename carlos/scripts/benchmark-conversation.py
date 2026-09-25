#!/usr/bin/env python3
"""Silent local synthetic speech benchmark; does not record or play audio."""

import asyncio
import copy
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
os.environ["PYTHONPATH"] = sys.path[0]
from ev.config import DEFAULT_CONFIG
from ev.voice.tts import TtsRouter
from ev.voice.stt import WhisperCppAdapter
from ev.voice.neural_vad import NeuralVadWorker


async def main():
    config = copy.deepcopy(DEFAULT_CONFIG["voice"])
    tts = TtsRouter(config["tts"])
    vad = NeuralVadWorker(config["vad"])
    phrases = [
        "Could you help me create cybersecurity tools for my own computer?",
        "I was thinking about learning Python, but I don't know where to start. What would you suggest?",
        "No, I meant the first idea we were talking about, not the second one.",
    ]
    try:
        await vad.start()
        with tempfile.TemporaryDirectory(prefix="ev-speech-benchmark-") as directory:
            adapters = []
            for model in ("ggml-base.en.bin", "ggml-small.en-q5_1.bin"):
                settings = {
                    **config["stt"],
                    "persistent_server": False,
                    "model": str(Path(config["stt"]["model"]).with_name(model)),
                }
                adapters.append(WhisperCppAdapter(settings, Path(directory)))
            for index, phrase in enumerate(phrases):
                audio = await tts.synthesize(phrase)
                conversion = await asyncio.create_subprocess_exec(
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "s16le",
                    "-ar",
                    str(audio.sample_rate),
                    "-ac",
                    "1",
                    "-i",
                    "pipe:0",
                    "-ar",
                    "16000",
                    "-f",
                    "s16le",
                    "pipe:1",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                )
                pcm, _ = await conversion.communicate(audio.pcm)
                start = time.perf_counter()
                probabilities = [
                    await vad.analyze(pcm[offset : offset + 1600], str(index))
                    for offset in range(0, len(pcm), 1600)
                ]
                print(
                    json.dumps(
                        {
                            "vad_ms": round((time.perf_counter() - start) * 1000, 1),
                            "speech_seconds": len(pcm) / 32000,
                            "max_probability": max(
                                (p for p in probabilities if p is not None), default=None
                            ),
                            "vad_error": vad.error,
                        }
                    ),
                    flush=True,
                )
                for adapter in adapters:
                    result = await adapter.transcribe(pcm)
                    print(
                        json.dumps(
                            {
                                "expected": phrase,
                                "transcript": result.raw,
                                "model": result.model,
                                "latency_ms": result.latency_ms,
                            }
                        ),
                        flush=True,
                    )
    finally:
        await vad.close()
        await tts.close()


asyncio.run(main())
