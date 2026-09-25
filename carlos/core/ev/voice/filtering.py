from __future__ import annotations

import math
import sys
from array import array


class PcmHighPass:
    """Stateful second-order Butterworth high-pass for mono s16le PCM."""

    def __init__(self, cutoff_hz: float = 140.0, sample_rate: int = 16000) -> None:
        self.sample_rate = max(8000, int(sample_rate))
        self.cutoff_hz = max(0.0, min(float(cutoff_hz), self.sample_rate * 0.45))
        if self.cutoff_hz == 0.0:
            self.b0, self.b1, self.b2 = 1.0, 0.0, 0.0
            self.a1, self.a2 = 0.0, 0.0
        else:
            omega = 2.0 * math.pi * self.cutoff_hz / self.sample_rate
            cosine = math.cos(omega)
            alpha = math.sin(omega) / (2.0 / math.sqrt(2.0))
            a0 = 1.0 + alpha
            self.b0 = ((1.0 + cosine) / 2.0) / a0
            self.b1 = (-(1.0 + cosine)) / a0
            self.b2 = self.b0
            self.a1 = (-2.0 * cosine) / a0
            self.a2 = (1.0 - alpha) / a0
        self.reset()

    def reset(self) -> None:
        self._x1 = 0.0
        self._x2 = 0.0
        self._y1 = 0.0
        self._y2 = 0.0

    def process(self, pcm: bytes) -> bytes:
        usable = len(pcm) - len(pcm) % 2
        if usable == 0 or self.cutoff_hz == 0.0:
            return pcm[:usable]
        samples = array("h")
        samples.frombytes(pcm[:usable])
        if sys.byteorder != "little":
            samples.byteswap()
        output = array("h")
        append = output.append
        x1, x2, y1, y2 = self._x1, self._x2, self._y1, self._y2
        for sample in samples:
            value = float(sample)
            filtered = self.b0 * value + self.b1 * x1 + self.b2 * x2 - self.a1 * y1 - self.a2 * y2
            append(max(-32768, min(32767, int(round(filtered)))))
            x2, x1 = x1, value
            y2, y1 = y1, filtered
        self._x1, self._x2, self._y1, self._y2 = x1, x2, y1, y2
        if sys.byteorder != "little":
            output.byteswap()
        return output.tobytes()
