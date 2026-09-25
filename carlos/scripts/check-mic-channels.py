#!/usr/bin/env python3
"""Explicit short local mic comparison; no playback, host actions or saved audio."""

import argparse
import asyncio
import json
import math
import os
import sys
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from ev.voice.filtering import PcmHighPass
from ev.voice.stt import WhisperCppAdapter
from ev.voice.neural_vad import NeuralVadWorker


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=12)
    args = parser.parse_args()
    config = json.loads(Path.home().joinpath(".config/ev/config.json").read_text())["voice"]
    query = await asyncio.create_subprocess_exec(
        "pactl", "get-default-source", stdout=asyncio.subprocess.PIPE
    )
    source = (await query.communicate())[0].decode().strip()
    if not source or source.endswith(".monitor"):
        raise RuntimeError("Select a microphone, not an output monitor")
    capture = await asyncio.create_subprocess_exec(
        "parec",
        "--raw",
        "--format=s16le",
        "--rate=16000",
        "--channels=2",
        "--channel-map=front-left,front-right",
        "--latency-msec=20",
        f"--device={source}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    vad = NeuralVadWorker(config["vad"])
    stt = WhisperCppAdapter(config["stt"], Path(f"/run/user/{os.getuid()}/ev"))
    try:
        seconds = max(3, min(20, args.seconds))
        print(
            json.dumps(
                {"status": "LISTENING", "source": source, "seconds": seconds, "audio_saved": False}
            ),
            flush=True,
        )
        raw = await asyncio.wait_for(capture.stdout.readexactly(seconds * 64000), seconds + 5)
        capture.terminate()
        await capture.wait()
        samples = array("h", raw)
        if sys.byteorder != "little":
            samples.byteswap()
        left, right = samples[::2], samples[1::2]
        mono = array("h", (int((l + r) / 2) for l, r in zip(left, right)))
        energy = lambda s: sum(x * x for x in s)
        correlation = sum(l * r for l, r in zip(left, right)) / max(
            1, math.sqrt(energy(left) * energy(right))
        )
        print(json.dumps({"channel_correlation": round(correlation, 4)}), flush=True)
        for name, channel in (("mono", mono), ("left", left), ("right", right)):
            rms = math.sqrt(energy(channel) / len(channel)) / 32768
            if sys.byteorder != "little":
                channel.byteswap()
            pcm = PcmHighPass(config.get("input_highpass_hz", 140)).process(channel.tobytes())
            probabilities = [
                await vad.analyze(pcm[i : i + 3200], name) for i in range(0, len(pcm), 3200)
            ]
            values = [p for p in probabilities if p is not None]
            result = await stt.transcribe(pcm)
            print(
                json.dumps(
                    {
                        "channel": name,
                        "rms": round(rms, 5),
                        "speech_max": max(values, default=0),
                        "speech_frames": sum(p >= 0.6 for p in values),
                        "frames": len(values),
                        "text": result.raw,
                    }
                ),
                flush=True,
            )
    finally:
        if capture.returncode is None:
            capture.kill()
            await capture.wait()
        await vad.close()
        await stt.close()


asyncio.run(main())
