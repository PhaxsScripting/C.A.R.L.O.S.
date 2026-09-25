#!/usr/bin/env python3
"""Silent synthetic phrase check for the installed local keyword model."""

import asyncio
import copy
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
os.environ["PYTHONPATH"] = sys.path[0]
from ev.config import DEFAULT_CONFIG
from ev.voice.tts import TtsRouter
from ev.voice.wake import WakeWordWorker


async def main():
    config = copy.deepcopy(DEFAULT_CONFIG["voice"])
    config["wake"]["keywords"] = str(
        Path(__file__).resolve().parents[1] / "assets/voice/keywords.txt"
    )
    tts = TtsRouter(config["tts"])
    try:
        for phrase in sys.argv[1:] or (
            "Hey Evie, open Firefox.",
            "Hey Eve, what time is it?",
            "Hey E.V., open Firefox.",
            "E.V., open Firefox.",
            "Evie, open Firefox.",
            "Eve, open Firefox.",
            "Every evening we leave the television on.",
            "Believe me, this movie is amazing.",
            "Never give up, keep singing with me.",
        ):
            detected = []
            worker = WakeWordWorker(config["wake"])

            async def on_detect(message):
                detected.append(message["keyword"])

            try:
                await worker.start(on_detect, lambda *_args: None)
                audio = await tts.synthesize(phrase)
                converter = await asyncio.create_subprocess_exec(
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
                pcm, _ = await converter.communicate(audio.pcm)
                await worker.feed(b"\0" * 16000 + pcm + b"\0" * 48000)
                await asyncio.sleep(2.0)
                print(
                    json.dumps(
                        {
                            "phrase": phrase,
                            "wake_results": detected,
                            "error": worker.last_error,
                            "worker_health": worker.health,
                        }
                    ),
                    flush=True,
                )
                if worker.processed_bytes <= 0:
                    raise RuntimeError("Wake worker did not acknowledge synthetic audio")
            finally:
                await worker.stop()
    finally:
        await tts.close()


asyncio.run(main())
