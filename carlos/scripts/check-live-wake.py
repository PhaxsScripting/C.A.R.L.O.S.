#!/usr/bin/env python3
"""Explicit, fixed-duration local mic check. No commands, playback or audio files.

Use only with the user's knowledge. Start first, then ask for the wake phrase.
Audio is streamed to an isolated candidate detector and discarded after the run.
The --compare file enables a same-audio comparison against previous keywords.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
os.environ["PYTHONPATH"] = str(ROOT / "core")
from ev.config import DEFAULT_CONFIG
from ev.voice.filtering import PcmHighPass
from ev.voice.wake import WakeWordWorker
from ev.voice.stt import WhisperCppAdapter


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--compare", type=Path)
    parser.add_argument(
        "--transcribe",
        action="store_true",
        help="Also transcribe the bounded sample locally for diagnosis",
    )
    parser.add_argument(
        "--current-config",
        action="store_true",
        help="Use current voice runtime/filter settings instead of repository defaults",
    )
    args = parser.parse_args()
    seconds = max(5, min(45, args.seconds))
    workers = []
    results = {}
    capture = None
    frames = 0
    audio = bytearray()
    voice_config = copy.deepcopy(DEFAULT_CONFIG["voice"])
    if args.current_config:
        from ev.config import _merge

        voice_config = _merge(
            voice_config,
            json.loads(Path.home().joinpath(".config/ev/config.json").read_text())["voice"],
        )
    try:
        profiles = {"candidate": ROOT / "assets/voice/keywords.txt"}
        if args.compare:
            profiles["previous"] = args.compare
        for label, keywords in profiles.items():
            config = copy.deepcopy(voice_config["wake"])
            config["keywords"] = str(keywords)
            worker = WakeWordWorker(config)
            results[label] = []

            async def detected(message, profile=label):
                result = {
                    "profile": profile,
                    "keyword": message["keyword"],
                    "elapsed": round(time.monotonic() - started, 2),
                }
                results[profile].append(result)
                print(json.dumps(result), flush=True)

            await worker.start(detected, lambda *_args: None)
            workers.append(worker)
        source_process = await asyncio.create_subprocess_exec(
            "/usr/bin/pactl",
            "get-default-source",
            stdout=asyncio.subprocess.PIPE,
        )
        source_raw, _ = await asyncio.wait_for(source_process.communicate(), 3)
        source = source_raw.decode().strip()
        if not source or source.endswith(".monitor"):
            raise RuntimeError("A real microphone must be selected, not an output monitor")
        capture = await asyncio.create_subprocess_exec(
            "/usr/bin/parec",
            "--raw",
            "--format=s16le",
            "--rate=16000",
            "--channels=1",
            "--latency-msec=20",
            f"--device={source}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        audio_filter = PcmHighPass(voice_config["input_highpass_hz"])
        started = time.monotonic()
        print(
            json.dumps(
                {
                    "status": "LISTENING",
                    "seconds": seconds,
                    "source": source,
                    "executes_commands": False,
                    "audio_saved": False,
                }
            ),
            flush=True,
        )
        while time.monotonic() - started < seconds:
            chunk = await asyncio.wait_for(capture.stdout.read(1600), 3)
            if not chunk:
                raise RuntimeError("Microphone stream ended")
            frames += len(chunk) // 2
            pcm = audio_filter.process(chunk)
            if args.transcribe:
                audio.extend(pcm)
            await asyncio.gather(*(worker.feed(pcm) for worker in workers))
        capture.terminate()
        await asyncio.wait_for(capture.wait(), 3)
        # Flush look-ahead, without retaining microphone audio.
        await asyncio.gather(*(worker.feed(b"\0" * 48000) for worker in workers))
        await asyncio.sleep(1)
        print(
            json.dumps(
                {
                    "status": "FINISHED",
                    "audio_seconds": round(frames / 16000, 2),
                    "results": results,
                }
            ),
            flush=True,
        )
        if args.transcribe:
            config = json.loads(Path.home().joinpath(".config/ev/config.json").read_text())
            # Reuse the live core's local STT server without taking ownership.
            adapter = WhisperCppAdapter(
                config["voice"]["stt"], Path(f"/tmp/ev-runtime-{os.getuid()}")
            )
            try:
                result = await adapter.transcribe(bytes(audio))
                print(
                    json.dumps(
                        {
                            "status": "TRANSCRIBED",
                            "text": result.raw,
                            "latency_ms": result.latency_ms,
                        }
                    ),
                    flush=True,
                )
            finally:
                audio.clear()
                await adapter.close()
    finally:
        if capture is not None and capture.returncode is None:
            capture.kill()
            await capture.wait()
        await asyncio.gather(*(worker.stop() for worker in workers), return_exceptions=True)


asyncio.run(main())
