from __future__ import annotations

from ev.platform import executable as _platform_executable

import asyncio
import io
import json
import os
import shutil
import time
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..telemetry import read_temperature


@dataclass(slots=True)
class SynthesizedAudio:
    pcm: bytes
    sample_rate: int
    channels: int
    sample_width: int
    engine: str
    voice: str
    latency_ms: float


class TtsAdapter(Protocol):
    @property
    def available(self) -> tuple[bool, str]: ...

    async def synthesize(self, text: str) -> SynthesizedAudio: ...


class PiperAdapter:
    """Neural TTS with a restartable resident worker and one-shot fallback."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self._worker: asyncio.subprocess.Process | None = None
        self._worker_lock = asyncio.Lock()

    @property
    def python(self) -> Path:
        return Path(str(self.config.get("python", ""))).expanduser()

    @property
    def model(self) -> Path:
        return Path(str(self.config.get("model", ""))).expanduser()

    @property
    def model_config(self) -> Path:
        configured = str(self.config.get("model_config", "")).strip()
        return Path(configured).expanduser() if configured else Path(f"{self.model}.json")

    @property
    def voice(self) -> str:
        return str(self.config.get("voice", self.model.stem))

    @property
    def available(self) -> tuple[bool, str]:
        if not self.python.is_file() or not os.access(self.python, os.X_OK):
            return False, f"Piper Python runtime is unavailable: {self.python}"
        if not self.model.is_file():
            return False, f"Piper voice model is unavailable: {self.model}"
        if not self.model_config.is_file():
            return False, f"Piper voice config is unavailable: {self.model_config}"
        return True, f"Piper voice {self.voice} is ready"

    async def prewarm(self) -> None:
        if bool(self.config.get("persistent_worker", True)):
            async with self._worker_lock:
                await self._ensure_worker()

    async def _ensure_worker(self) -> asyncio.subprocess.Process:
        from ev.platform import IS_FREEBSD

        if self._worker is not None and self._worker.returncode is None:
            return self._worker
        environment = os.environ.copy()
        core_root = str(Path(__file__).resolve().parents[2])
        environment["PYTHONPATH"] = core_root + (
            os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else ""
        )
        process = await asyncio.create_subprocess_exec(
            _platform_executable("/usr/bin/nice"),
            "-n",
            str(int(self.config.get("nice", 15))),
            str(self.python),
            "-m",
            "ev.platform.piper_worker" if IS_FREEBSD else "ev.voice.tts_worker",
            "--model",
            str(self.model),
            "--config",
            str(self.model_config),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=environment,
            start_new_session=True,
        )
        try:
            assert process.stdout is not None
            raw_ready = await asyncio.wait_for(
                process.stdout.readline(),
                timeout=float(self.config.get("worker_start_timeout_seconds", 15)),
            )
            ready = json.loads(raw_ready)
            if ready.get("type") != "ready":
                raise RuntimeError("Piper worker did not become ready")
        except BaseException:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            raise
        self._worker = process
        return process

    async def _synthesize_persistent(self, text: str) -> SynthesizedAudio:
        async with self._worker_lock:
            process = await self._ensure_worker()
            assert process.stdin is not None and process.stdout is not None
            request_id = uuid.uuid4().hex
            request = {
                "id": request_id,
                "text": text.strip(),
                "speaking_rate": float(self.config.get("speaking_rate", 1.0)),
                "noise_scale": float(self.config.get("noise_scale", 0.62)),
                "noise_w_scale": float(self.config.get("noise_w_scale", 0.72)),
                "sentence_silence": float(self.config.get("sentence_silence", 0.16)),
                "volume": float(self.config.get("volume", 0.9)),
            }
            process.stdin.write(json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n")
            await process.stdin.drain()
            timeout = float(self.config.get("timeout_seconds", 30))
            raw_header = await asyncio.wait_for(process.stdout.readline(), timeout=timeout)
            header = json.loads(raw_header)
            if header.get("id") != request_id:
                raise RuntimeError("Piper worker returned a mismatched response")
            if header.get("type") == "error":
                raise RuntimeError(
                    f"Piper synthesis failed: {str(header.get('error', 'unknown error'))[:500]}"
                )
            byte_count = int(header.get("bytes", 0))
            if (
                header.get("type") != "audio"
                or byte_count <= 0
                or byte_count > 20_000_000
                or byte_count % 2
            ):
                raise RuntimeError("Piper worker returned an invalid audio header")
            pcm = await asyncio.wait_for(process.stdout.readexactly(byte_count), timeout=timeout)
            return SynthesizedAudio(
                pcm=pcm,
                sample_rate=int(header["sample_rate"]),
                channels=int(header.get("channels", 1)),
                sample_width=int(header.get("sample_width", 2)),
                engine="piper",
                voice=self.voice,
                latency_ms=float(header.get("latency_ms", 0.0)),
            )

    async def _synthesize_oneshot(self, text: str) -> SynthesizedAudio:
        # A fresh bounded-thread worker keeps failure recovery from restoring
        # the CLI's automatic CPU thread pool.
        temporary = PiperAdapter(self.config)
        try:
            return await temporary._synthesize_persistent(text)
        finally:
            await temporary.close()

    async def synthesize(self, text: str) -> SynthesizedAudio:
        available, reason = self.available
        if not available:
            raise RuntimeError(reason)
        if bool(self.config.get("persistent_worker", True)):
            try:
                return await self._synthesize_persistent(text)
            except asyncio.CancelledError:
                # A cancelled read leaves audio/header bytes in the pipe. Never
                # reuse that stream for the next spoken response.
                await self.close()
                raise
            except (
                OSError,
                EOFError,
                asyncio.IncompleteReadError,
                asyncio.TimeoutError,
                json.JSONDecodeError,
                RuntimeError,
                ValueError,
            ):
                await self.close()
        return await self._synthesize_oneshot(text)

    async def close(self) -> None:
        process = self._worker
        self._worker = None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except TimeoutError:
            process.kill()
            await process.wait()


class EspeakAdapter:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    @property
    def available(self) -> tuple[bool, str]:
        ready = shutil.which("espeak-ng") is not None
        return ready, "eSpeak NG fallback is ready" if ready else "eSpeak NG is unavailable"

    async def synthesize(self, text: str) -> SynthesizedAudio:
        started = time.monotonic()
        voice = str(self.config.get("fallback_voice", "en-us+f3"))
        process = await asyncio.create_subprocess_exec(
            _platform_executable("/usr/bin/espeak-ng"),
            "--stdout",
            "-v",
            voice,
            "--stdin",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            wav_data, stderr = await asyncio.wait_for(
                process.communicate(text.strip().encode("utf-8")), timeout=30
            )
        except BaseException:
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            raise
        if process.returncode != 0:
            raise RuntimeError(f"eSpeak NG failed: {stderr.decode(errors='replace')[:300]}")
        with wave.open(io.BytesIO(wav_data), "rb") as reader:
            channels = reader.getnchannels()
            rate = reader.getframerate()
            width = reader.getsampwidth()
            pcm = reader.readframes(reader.getnframes())
        return SynthesizedAudio(
            pcm,
            rate,
            channels,
            width,
            "espeak-ng",
            voice,
            (time.monotonic() - started) * 1000,
        )


class TtsRouter:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.piper = PiperAdapter(config)
        self.espeak = EspeakAdapter(config)

    @property
    def selected(self) -> str:
        return str(self.config.get("provider", "piper"))

    @property
    def adapter(self) -> TtsAdapter:
        if self.selected == "piper":
            return self.piper
        return self.espeak

    @property
    def available(self) -> tuple[bool, str]:
        return self.adapter.available

    @property
    def status(self) -> dict[str, Any]:
        available, reason = self.available
        adapter = self.adapter
        voice = (
            adapter.voice
            if isinstance(adapter, PiperAdapter)
            else str(self.config.get("fallback_voice", "en-us+f3"))
        )
        return {
            "provider": "piper" if isinstance(adapter, PiperAdapter) else "espeak-ng",
            "configured_provider": self.selected,
            "voice": voice,
            "available": available,
            "reason": reason,
            "speaking_rate": float(self.config.get("speaking_rate", 1.0)),
            "volume": float(self.config.get("volume", 0.9)),
            "pitch": None,
            "expressiveness": float(self.config.get("noise_scale", 0.62)),
            "output_device": str(self.config.get("output_device", "@DEFAULT_SINK@")),
        }

    async def synthesize(self, text: str) -> SynthesizedAudio:
        adapter = self.adapter
        if isinstance(adapter, PiperAdapter):
            temperature = read_temperature().get("celsius")
            limit = float(self.config.get("thermal_pause_celsius", 97.0))
            if temperature is not None and float(temperature) >= limit:
                raise RuntimeError(
                    f"Voice paused at {float(temperature):.0f} C to let the CPU cool. The reply is available in chat."
                )
        return await adapter.synthesize(text)

    async def prewarm(self) -> None:
        if isinstance(self.adapter, PiperAdapter):
            await self.piper.prewarm()

    async def close(self) -> None:
        await self.piper.close()
