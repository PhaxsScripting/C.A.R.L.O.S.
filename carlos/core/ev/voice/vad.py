from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class VadUpdate:
    active: bool
    speech_started: bool
    speech_ended: bool
    threshold: float
    noise_floor: float
    speech_ms: float
    silence_ms: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "speech_started": self.speech_started,
            "speech_ended": self.speech_ended,
            "threshold": round(self.threshold, 5),
            "noise_floor": round(self.noise_floor, 5),
            "speech_ms": round(self.speech_ms, 1),
            "silence_ms": round(self.silence_ms, 1),
        }


class EnergyVad:
    """Small adaptive VAD used for capture boundaries and diagnostics."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        settings = config or {}
        self.minimum_threshold = float(settings.get("minimum_rms", 0.010))
        self.noise_ratio = float(settings.get("noise_ratio", 3.0))
        self.start_ms = float(settings.get("start_ms", 180))
        self.end_silence_ms = float(settings.get("end_silence_ms", 1600))
        responsive_end = float(settings.get("responsive_end_silence_ms", 1600))
        self.endpoint_silence_ms = min(self.end_silence_ms, max(600.0, responsive_end))
        self.minimum_speech_ms = float(settings.get("minimum_speech_ms", 300))
        self.maximum_speech_ms = float(settings.get("maximum_speech_ms", 15000))
        self.maximum_noise_floor = float(settings.get("maximum_noise_floor", 0.020))
        self.speech_probability = float(settings.get("speech_probability", 0.45))
        self.continuation_probability = float(settings.get("continuation_probability", 0.25))
        self.reset()

    def reset(self, noise_floor: float = 0.0) -> None:
        self.noise_floor = min(self.maximum_noise_floor, max(0.0, float(noise_floor)))
        self.active_run_ms = 0.0
        self.speech_ms = 0.0
        self.silence_ms = 0.0
        self.speech_active = False
        self.ended = False
        self.frames = 0
        self.elapsed_speech_ms = 0.0

    def update(
        self, rms: float, peak: float, frame_ms: float, probability: float | None = None
    ) -> VadUpdate:
        self.frames += 1
        threshold = max(self.minimum_threshold, self.noise_floor * self.noise_ratio)
        # Hysteresis retains quieter syllables after speech has started. When
        # available, the local neural detector distinguishes speech from fans
        # instead of assuming every loud signal is a voice.
        continuation = threshold * 0.65 if self.speech_active else threshold
        frame_active = rms >= continuation or peak >= max(0.025, continuation * 3.0)
        if probability is not None:
            frame_active = probability >= (
                self.continuation_probability if self.speech_active else self.speech_probability
            )

        if not self.speech_active and not frame_active:
            rate = 0.18 if self.frames < 20 else 0.025
            self.noise_floor = (
                rms if self.noise_floor == 0.0 else (1.0 - rate) * self.noise_floor + rate * rms
            )
            self.noise_floor = min(self.maximum_noise_floor, self.noise_floor)
            threshold = max(self.minimum_threshold, self.noise_floor * self.noise_ratio)

        started = False
        ended = False
        if not self.speech_active:
            self.active_run_ms = self.active_run_ms + frame_ms if frame_active else 0.0
            if self.active_run_ms >= self.start_ms:
                self.speech_active = True
                started = True
                self.speech_ms = self.active_run_ms
                self.elapsed_speech_ms = self.active_run_ms
                self.silence_ms = 0.0
        else:
            self.elapsed_speech_ms += frame_ms
            if frame_active:
                self.speech_ms += frame_ms
                self.silence_ms = 0.0
            else:
                self.silence_ms += frame_ms
            if (
                self.speech_ms >= self.minimum_speech_ms
                and self.silence_ms >= self.endpoint_silence_ms
            ) or self.elapsed_speech_ms >= self.maximum_speech_ms:
                self.ended = True
                ended = True

        return VadUpdate(
            active=self.speech_active and not self.ended,
            speech_started=started,
            speech_ended=ended,
            threshold=threshold,
            noise_floor=self.noise_floor,
            speech_ms=self.speech_ms,
            silence_ms=self.silence_ms,
        )
