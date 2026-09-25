#!/usr/bin/env python3
"""Compare short-command prompts with synthesized audio; no capture or playback."""

import asyncio
import copy
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from ev.config import DEFAULT_CONFIG
from ev.voice.stt import WhisperCppAdapter
from ev.voice.tts import TtsRouter
from ev.voice.normalization import normalize_transcript, interpret_spoken_command
from ev.commands import media_request


async def main():
    config = copy.deepcopy(DEFAULT_CONFIG["voice"])
    config["stt"]["persistent_server"] = False
    tts = TtsRouter(config["tts"])
    prompts = [
        "E.V., Phaxity Neko Music, Spotify, Firefox, Discord, RAM, CPU, GPU.",
        config["stt"]["prompt"],
    ]
    with tempfile.TemporaryDirectory(prefix="ev-media-speech-") as directory:
        stt = WhisperCppAdapter(config["stt"], Path(directory))
        try:
            for phrase in (
                "Pause my music.",
                "Pause Spotify.",
                "Unpause my Spotify, bro, what the fuck.",
                "What is plasma made of?",
            ):
                audio = await tts.synthesize(phrase)
                converter = await asyncio.create_subprocess_exec(
                    "ffmpeg",
                    "-v",
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
                for index, prompt in enumerate(prompts):
                    stt.config["prompt"] = prompt
                    transcript = await stt.transcribe(pcm)
                    interpretation = interpret_spoken_command(
                        normalize_transcript(transcript.raw), media_active=True
                    )
                    action = media_request(interpretation.text)
                    print(
                        json.dumps(
                            {
                                "phrase": phrase,
                                "prompt": index,
                                "raw": transcript.raw,
                                "interpreted": interpretation.text,
                                "media_action": action.arguments if action else None,
                                "stt_ms": round(transcript.latency_ms),
                            }
                        ),
                        flush=True,
                    )
        finally:
            await stt.close()
            await tts.close()


asyncio.run(main())
