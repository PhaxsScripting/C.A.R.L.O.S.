#!/usr/bin/env python3
"""Silent synthetic recognition comparison; no microphone or executed commands."""

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from ev.voice.stt import WhisperCppAdapter
from ev.voice.tts import TtsRouter


async def main():
    config = json.loads(Path.home().joinpath(".config/ev/config.json").read_text())["voice"]
    tts = TtsRouter(config["tts"])
    samples = []
    try:
        for text in (
            "Hey Carlos, what is my CPU usage?",
            "Open Firefox on the big monitor.",
            "Every evening we leave the television on.",
        ):
            audio = await tts.synthesize(text)
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
            if converter.returncode:
                raise RuntimeError("Synthetic sample conversion failed")
            samples.append((text, pcm + bytes(16000)))
    finally:
        await tts.close()
    for label, override in (
        ("base-beam3", {"beam_size": 3}),
        ("base-beam1", {"beam_size": 1}),
        ("tiny-preview", config["preview_stt"]),
    ):
        # Own a separate endpoint and ownership record; never close the live core worker.
        with tempfile.TemporaryDirectory(
            prefix="carlos-speech-bench-", dir=f"/run/user/{os.getuid()}"
        ) as directory:
            stt = WhisperCppAdapter(
                {**config["stt"], **override, "server_port": 18084}, Path(directory)
            )
            try:
                await stt.prewarm()
                for expected, pcm in samples:
                    started = time.monotonic()
                    result = await stt.transcribe(pcm)
                    print(
                        json.dumps(
                            {
                                "configuration": label,
                                "expected": expected,
                                "recognized": result.raw,
                                "wall_ms": round((time.monotonic() - started) * 1000, 1),
                                "audio_ms": len(pcm) / 32,
                            }
                        ),
                        flush=True,
                    )
            finally:
                await stt.close()


asyncio.run(main())
