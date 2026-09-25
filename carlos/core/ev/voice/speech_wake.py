"""Bounded speech-triggered local wake backup; no chat model or host actions."""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from typing import Any, Awaitable, Callable

from .normalization import normalize_transcript

# Match the same standalone Eve pronunciation as the primary keyword model.
# Keep the leading-name boundary: evening/every/embedded mentions cannot wake.
_NAME = re.compile(
    r"^\s*(?:(?:hey|yo)[\s,!.:-]+)?(?:carlos|eevee|evee|evie|eve|e[.\s-]*v\.?)\b", re.I
)


class SpeechWakeFallback:
    def __init__(
        self,
        vad: Any,
        stt: Any,
        allowed: Callable[[], bool],
        detected: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        speech_probability: float = 0.45,
    ) -> None:
        self.vad, self.stt, self.allowed, self.detected = vad, stt, allowed, detected
        # This gate only selects audio for local transcription. The exact
        # leading name remains mandatory before waking or running anything.
        self.speech_probability = max(0.25, min(0.8, float(speech_probability)))
        self.task: asyncio.Task[None] | None = None
        self.pre_roll: deque[bytes] = deque(maxlen=3)
        self.pcm = bytearray()
        self.tail = bytearray()
        self.speech_ms = 0.0
        self.silence_ms = 0.0
        self.next_check = 0.0
        self.status = "IDLE"
        self.checks = 0
        self.matches = 0
        self.frames_analyzed = 0
        self.speech_frames = 0
        self.last_probability: float | None = None
        self.max_probability = 0.0
        self.rejected_segments = 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.status,
            "checks": self.checks,
            "matches": self.matches,
            "frames_analyzed": self.frames_analyzed,
            "speech_frames": self.speech_frames,
            "speech_probability": self.last_probability,
            "max_speech_probability": self.max_probability,
            "speech_threshold": self.speech_probability,
            "rejected_segments": self.rejected_segments,
        }

    def reset(self) -> None:
        self.pre_roll.clear()
        self.pcm.clear()
        self.tail.clear()
        self.speech_ms = self.silence_ms = 0.0

    async def feed(self, pcm: bytes) -> None:
        self.last_probability = None
        if not self.allowed():
            self.reset()
            return
        if self.task is not None and not self.task.done():
            # Retain speech that follows the name while STT checks it. A
            # bounded tail prevents losing "open Firefox" behind inference.
            self.tail.extend(pcm)
            del self.tail[:-96000]
            return
        if time.monotonic() < self.next_check:
            return
        probability = await self.vad.analyze(pcm, "wake-speech-backup")
        if not self.allowed():
            self.reset()
            return
        if probability is None:
            self.status = "VAD_UNAVAILABLE"
            self.reset()
            return
        self.last_probability = probability
        self.max_probability = max(self.max_probability, probability)
        self.frames_analyzed += 1
        self.speech_frames += int(probability >= self.speech_probability)
        self.status = "LISTENING"
        duration_ms = len(pcm) / 32
        speech = probability >= self.speech_probability
        if not self.pcm:
            self.pre_roll.append(pcm)
            if not speech:
                return
            self.pcm.extend(b"".join(self.pre_roll))
            self.pre_roll.clear()
        else:
            self.pcm.extend(pcm)
        self.speech_ms += duration_ms if speech else 0
        self.silence_ms = 0 if speech else self.silence_ms + duration_ms
        if len(self.pcm) >= 96000 or self.silence_ms >= 400:
            sample = bytes(self.pcm[:96000])
            enough_speech = self.speech_ms >= 100
            self.reset()
            if enough_speech:
                self.next_check = time.monotonic() + 4.0
                self.task = asyncio.create_task(self._check(sample))
            else:
                self.rejected_segments += 1

    async def _check(self, pcm: bytes) -> None:
        self.status = "CHECKING"
        self.checks += 1
        try:
            transcript = await asyncio.wait_for(self.stt.transcribe(pcm), 8)
            if self.allowed() and _NAME.match(transcript.raw):
                self.matches += 1
                self.status = "DETECTED"
                await self.detected(
                    {
                        "keyword": (
                            "HEY_CARLOS"
                            if re.match(r"\s*hey\b", transcript.raw, re.I)
                            else "CARLOS"
                        ),
                        "source": "local_speech_backup",
                        "seed_pcm": pcm + bytes(self.tail),
                        "seed_is_command": bool(normalize_transcript(transcript.raw)),
                    }
                )
            else:
                self.status = "LISTENING"
        except asyncio.CancelledError:
            self.status = "CANCELLED"
            raise
        except Exception:
            self.status = "RETRY_LATER"
            self.next_check = time.monotonic() + 10
        finally:
            self.tail.clear()

    async def close(self) -> None:
        task, self.task = self.task, None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.reset()
