#!/usr/bin/env python3
"""Synthetic, silent neural VAD -> local STT -> wake check. No host actions."""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
os.environ["PYTHONPATH"] = sys.path[0]
from ev.voice.tts import TtsRouter
from ev.voice.neural_vad import NeuralVadWorker
from ev.voice.stt import WhisperCppAdapter
from ev.voice.speech_wake import SpeechWakeFallback


async def main():
    config = json.loads(Path.home().joinpath(".config/ev/config.json").read_text())["voice"]
    tts = TtsRouter(config["tts"])
    vad = NeuralVadWorker(config["vad"])
    stt = WhisperCppAdapter(config["stt"], Path(f"/tmp/ev-runtime-{os.getuid()}"))
    try:
        for phrase, expected in (
            ("Hey, Eevee, what time is it?", True),
            ("Eevee.", True),
            ("Every evening we leave the television on.", False),
            ("Believe me, this movie is amazing.", False),
        ):
            matches = []

            async def detected(message):
                matches.append(
                    {"keyword": message["keyword"], "has_command": message["seed_is_command"]}
                )

            wake = SpeechWakeFallback(vad, stt, lambda: True, detected)
            try:
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
                started = time.monotonic()
                for offset in range(0, len(pcm) + 32000, 3200):
                    await wake.feed(
                        pcm[offset : offset + 3200] if offset < len(pcm) else bytes(3200)
                    )
                if wake.task:
                    await wake.task
                print(
                    json.dumps(
                        {
                            "phrase": phrase,
                            "expected_wake": expected,
                            "matched": bool(matches),
                            "matches": matches,
                            "check_ms": round((time.monotonic() - started) * 1000),
                            "checks": wake.checks,
                            "state": wake.status,
                        }
                    ),
                    flush=True,
                )
                if bool(matches) != expected:
                    raise RuntimeError(f"Synthetic wake mismatch: {phrase}")
            finally:
                await wake.close()
    finally:
        await tts.close()
        await vad.close()
        await stt.close()


asyncio.run(main())
