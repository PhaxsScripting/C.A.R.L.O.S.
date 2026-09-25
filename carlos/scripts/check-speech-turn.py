#!/usr/bin/env python3
"""Synthetic full capture/STT turn: no microphone, playback, or desktop action."""

import asyncio
import copy
import json
import os
import sys
import tempfile
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
os.environ["PYTHONPATH"] = sys.path[0]
from ev.config import DEFAULT_CONFIG
from ev.events import PhaxEventBus
from ev.state import StateMachine
from ev.voice import VoiceManager


async def main():
    with tempfile.TemporaryDirectory(prefix="ev-turn-check-") as directory:
        config = copy.deepcopy(DEFAULT_CONFIG["voice"])
        config["wake"]["enabled"] = False
        config["stt"]["persistent_server"] = False
        if len(sys.argv) > 1 and sys.argv[1].isdigit():
            config["stt"]["beam_size"] = int(sys.argv[1])
        bus = PhaxEventBus()
        manager = VoiceManager(config, bus, StateMachine(bus), Path(directory))
        received = []

        async def record_only(text, correlation):
            received.append(text)
            return {
                "status": "completed",
                "correlation_id": correlation,
                "response": "Test recorded. No actions.",
            }

        manager.set_command_handler(record_only)
        try:
            await manager.neural_vad.start()
            audio_parts = []
            for phrase in (
                "I want to learn Python, but I'm still a beginner.",
                "Could you help me make a simple guessing game with variables and loops?",
                "Please explain the first step calmly, because I only started learning yesterday.",
            ):
                audio = await manager.tts.synthesize(phrase)
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
                samples = array("h")
                samples.frombytes(pcm)
                quiet = array("h", (int(sample * 0.12) for sample in samples)).tobytes()
                audio_parts.append(quiet)
            # Begin near the old reply-window deadline, then speak a long turn
            # at 12% synthesized volume, with natural pauses between clauses.
            pcm = b"\0" * (7 * 32000) + (b"\0" * 32000).join(audio_parts) + b"\0" * (2 * 32000)
            await manager._activate_capture(
                "synthetic-long-turn", "follow_up", "ambient", "synthetic-no-microphone"
            )
            for offset in range(0, len(pcm), 1600):
                if await manager._analyze_capture_chunk(pcm[offset : offset + 1600]):
                    break
            reason = manager.capture_auto_reason
            bounded_pcm = bytes(
                manager.capture_pcm[
                    manager.capture_speech_start_byte or 0 : manager.capture_speech_end_byte
                ]
            )
            result = await manager.stop_capture("synthetic-long-turn")
            print(
                json.dumps(
                    {
                        "status": result["status"],
                        "reason": reason,
                        "transcripts": received,
                        "capture_ms": manager.diagnostics["last_capture_duration_ms"],
                        "audio_ms": manager.diagnostics["stt_audio_duration_ms"],
                        "stt_ms": manager.diagnostics["stt_latency_ms"],
                        "vad_engine": manager.diagnostics.get("vad_engine"),
                        "max_speech_probability": manager.diagnostics.get("max_speech_probability"),
                    },
                    indent=2,
                )
            )
            assert reason == "end_of_speech", reason
            assert received and "yesterday" in received[0].lower(), received
            assert manager.diagnostics["stt_audio_duration_ms"] > 8000
            if "--compare" in sys.argv:
                for beam in (1, 3, 5):
                    manager.stt.config["beam_size"] = beam
                    result = await manager.stt.transcribe(bounded_pcm)
                    print(
                        json.dumps(
                            {
                                "same_audio_beam": beam,
                                "text": result.raw,
                                "latency_ms": round(result.latency_ms),
                            },
                            indent=2,
                        ),
                        flush=True,
                    )
        finally:
            await manager.close()


asyncio.run(main())
