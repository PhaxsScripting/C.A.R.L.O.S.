"""Bounded, local-only speech probability worker; energy VAD is the fallback."""

from __future__ import annotations

from ev.platform import executable as _platform_executable

import asyncio
import base64
import json
import os
import time
from pathlib import Path
from typing import Any


class NeuralVadWorker:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.process: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self.retry_at = 0.0
        self.error = ""

    async def start(self) -> None:
        async with self.lock:
            await self._start()

    async def _start(self) -> None:
        if not self.config.get("neural_enabled", False) or time.monotonic() < self.retry_at:
            return
        if self.process is not None and self.process.returncode is None:
            return
        python = Path(str(self.config.get("python", ""))).expanduser()
        model = Path(str(self.config.get("model", ""))).expanduser()
        if not python.is_file() or not model.is_file():
            self.error = "Local speech detector is missing; using adaptive energy detection"
            self.retry_at = time.monotonic() + 30
            return
        try:
            self.process = await asyncio.create_subprocess_exec(
                _platform_executable("/usr/bin/nice"),
                "-n",
                "10",
                str(python),
                "-m",
                "ev.voice.neural_vad_worker",
                str(model),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
                env={**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"},
            )
            assert self.process.stdout is not None
            ready = await asyncio.wait_for(self.process.stdout.readline(), 5)
            if json.loads(ready).get("ready") is not True:
                raise RuntimeError("Local speech detector did not become ready")
            self.error = ""
        except (OSError, ValueError, RuntimeError, asyncio.TimeoutError) as error:
            self.error = f"Speech detector fallback: {type(error).__name__}"
            self.retry_at = time.monotonic() + 30
            await self.close()
        except asyncio.CancelledError:
            await self.close()
            raise

    async def analyze(self, pcm: bytes, capture_id: str) -> float | None:
        if not self.config.get("neural_enabled", False):
            return None
        async with self.lock:
            await self._start()
            process = self.process
            if process is None or process.returncode is not None:
                return None
            assert process.stdin is not None and process.stdout is not None
            try:
                request = json.dumps(
                    {"capture": capture_id, "pcm": base64.b64encode(pcm).decode("ascii")}
                )
                process.stdin.write(request.encode() + b"\n")
                await asyncio.wait_for(process.stdin.drain(), 0.5)
                line = await asyncio.wait_for(process.stdout.readline(), 0.5)
                probability = float(json.loads(line)["probability"])
                if not 0 <= probability <= 1:
                    raise ValueError("Invalid speech probability")
                return probability
            except (OSError, ValueError, KeyError, asyncio.TimeoutError) as error:
                self.error = f"Speech detector fallback: {type(error).__name__}"
                self.retry_at = time.monotonic() + 30
                await self.close()
                return None
            except asyncio.CancelledError:
                # Discard any unread response; it belongs to the old capture.
                await self.close()
                raise

    async def close(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), 1)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
