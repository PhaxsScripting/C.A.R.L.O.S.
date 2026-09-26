from __future__ import annotations

from ev.platform import executable as _platform_executable

import asyncio
import json
import math
import os
import shutil
import time
from array import array
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable
import uuid

from ..events import PhaxEventBus
from ..intents import extract_close_targets
from ..state import CoreState, StateMachine
from .filtering import PcmHighPass
from .normalization import is_conversation_stop, normalize_transcript, interpret_spoken_command
from .stt import WhisperCppAdapter
from .tts import TtsRouter
from .vad import EnergyVad
from .neural_vad import NeuralVadWorker
from .wake import WakeWordWorker
from .media_focus import MediaFocus
from .speech_wake import SpeechWakeFallback
from .echo import EchoCancel
from .streaming import speech_chunks, PartialTranscript

CommandHandler = Callable[[str, str], Awaitable[dict[str, Any]]]
ResponseHandler = Callable[[dict[str, Any]], None]


class VoiceManager:
    """On-demand audio capture and TTS. No microphone data leaves this process."""

    def __init__(
        self,
        config: dict[str, Any],
        bus: PhaxEventBus,
        state: StateMachine,
        runtime_dir: Path | None = None,
    ) -> None:
        self.config = config
        self.bus = bus
        self.state = state
        self.runtime_dir = runtime_dir or Path(f"/tmp/ev-runtime-{os.getuid()}")
        self.stt = WhisperCppAdapter(config.get("stt", {}), self.runtime_dir)
        self.tts = TtsRouter(config.get("tts", {}))
        self.vad = EnergyVad(config.get("vad", {}))
        self.neural_vad = NeuralVadWorker(config.get("vad", {}))
        self.wake = WakeWordWorker(config.get("wake", {}))
        self.echo = EchoCancel(bool(config.get("echo_cancel", False)))
        self._barge_speech_ms = 0.0
        preview_config = {
            **config.get("stt", {}),
            "server_port": int(config.get("stt", {}).get("server_port", 18082)) + 1,
            **config.get("preview_stt", {}),
        }
        self.preview_stt = WhisperCppAdapter(preview_config, self.runtime_dir / "preview")
        self.partial = PartialTranscript(self.preview_stt.transcribe, self._partial_transcript)
        self._wake_candidate_at = 0.0
        self._media_check_at = 0.0
        self._media_playing = False
        self.media_focus = MediaFocus(self._audio_listing, self._audio_env)
        self.media_focus_task: asyncio.Task[None] | None = None
        self.wake_chime_task: asyncio.Task[None] | None = None
        self.speech_wake = SpeechWakeFallback(
            self.neural_vad,
            self.preview_stt,
            self._speech_wake_allowed,
            self._on_wake_detected,
            speech_probability=float(config.get("vad", {}).get("speech_probability", 0.45)),
        )
        self.command_handler: CommandHandler | None = None
        self.response_handler: ResponseHandler | None = None
        self.interrupt_handler: Callable[[], Awaitable[bool]] | None = None
        self.capture_active = False
        self.capture_origin = ""
        self.capture_finishing = False
        self.capture_process: asyncio.subprocess.Process | None = None
        self.capture_task: asyncio.Task[None] | None = None
        self.auto_stop_task: asyncio.Task[None] | None = None
        self.capture_correlation_id = ""
        self.capture_mode = "command"
        self.capture_started = 0.0
        self.capture_bytes = 0
        self.capture_seed_bytes = 0
        self.capture_error = ""
        self.capture_pcm = bytearray()
        self.capture_auto_reason = ""
        self.capture_speech_start_byte: int | None = None
        self.capture_speech_end_byte: int | None = None
        self._capture_last_emit = 0.0
        self._capture_last_activity = False
        self.capture_invalid_input_ms = 0.0
        highpass_hz = float(config.get("input_highpass_hz", 140.0))
        self.wake_filter = PcmHighPass(highpass_hz)
        self.capture_filter = PcmHighPass(highpass_hz)
        self.wake_noise_rms: deque[float] = deque(maxlen=100)
        wake_frames = max(8, int(float(config.get("wake", {}).get("rolling_buffer_ms", 2200)) / 50))
        self.wake_buffer: deque[bytes] = deque(maxlen=wake_frames)
        gate_frames = max(3, int(float(config.get("wake", {}).get("gate_pre_roll_ms", 350)) / 50))
        self.wake_gate_buffer: deque[bytes] = deque(maxlen=gate_frames)
        self.wake_gate_active = False
        self.wake_gate_silence_ms = 0.0
        self.wake_invalid_input_ms = 0.0
        self.wake_loud_unvoiced_ms = 0.0
        self.wake_audio_process: asyncio.subprocess.Process | None = None
        self.wake_supervisor_task: asyncio.Task[None] | None = None
        self.wake_desired = bool(config.get("wake", {}).get("enabled", False))
        self.wake_paused = False
        self.resource_suspended = False
        self.privacy_mode = False
        self.wake_last_feed_at = 0.0
        self.wake_test_armed_until = 0.0
        self.wake_test_task: asyncio.Task[None] | None = None
        self.wake_test_max_rms = 0.0
        self.wake_test_max_peak = 0.0
        self.pipeline_test: dict[str, Any] = {}
        self.pipeline_test_started = 0.0
        self.last_input_level = {"rms": 0.0, "peak": 0.0, "waveform": []}
        self.diagnostics: dict[str, Any] = {
            "microphone": "",
            "pipewire": (
                "READY" if Path(f"/run/user/{os.getuid()}/pulse/native").exists() else "UNAVAILABLE"
            ),
            "capture_state": "IDLE",
            "sample_rate": 16000,
            "channels": 1,
            "pcm_format": "s16le",
            "input_highpass_hz": highpass_hz,
            "input_rms": 0.0,
            "input_peak": 0.0,
            "input_clip_ratio": 0.0,
            "input_dc_offset": 0.0,
            "max_rms": 0.0,
            "max_peak": 0.0,
            "max_clip_ratio": 0.0,
            "max_abs_dc_offset": 0.0,
            "max_speech_probability": 0.0,
            "voice_activity": False,
            "noise_floor": 0.0,
            "wake_state": "IDLE",
            "wake_error": "",
            "wake_keyword": "",
            "wake_detection_latency_ms": None,
            "wake_to_listening_ms": None,
            "last_capture_duration_ms": 0.0,
            "stt_audio_duration_ms": 0.0,
            "capture_health": "UNTESTED",
            "stt_state": "IDLE",
            "raw_transcript": "",
            "normalized_transcript": "",
            "detected_intent": "",
            "selected_tool": "",
            "permission_class": "",
            "tool_result": "",
            "tts_state": "IDLE",
            "tts_startup_latency_ms": None,
            "barge_in_latency_ms": None,
            "follow_up_state": "IDLE",
            "pipeline_test": {},
            "stt_latency_ms": None,
            "close_verification_state": "IDLE",
            "close_verification_model": "",
            "close_verification_latency_ms": None,
        }
        self.speaking = False
        self.generating_speech = False
        self.speech_pending = False
        self.tts_process: asyncio.subprocess.Process | None = None
        self.tts_cancel_reason = ""
        self._tts_lock = asyncio.Lock()
        self._capture_stop_lock = asyncio.Lock()

    def set_command_handler(self, handler: CommandHandler) -> None:
        self.command_handler = handler

    def set_response_handler(self, handler: ResponseHandler) -> None:
        self.response_handler = handler

    @property
    def capture_available(self) -> bool:
        command = self.config.get("capture_command", [])
        return bool(
            command and os.path.isfile(str(command[0])) and os.access(str(command[0]), os.X_OK)
        )

    @property
    def tts_available(self) -> bool:
        return self.tts.available[0] and shutil.which("paplay") is not None

    @property
    def stt_available(self) -> bool:
        return self.stt.available[0]

    @staticmethod
    def _audio_env() -> dict[str, str]:
        environment = os.environ.copy()
        native_socket = Path(f"/run/user/{os.getuid()}/pulse/native")
        configured_socket = Path(environment.get("XDG_RUNTIME_DIR", "")) / "pulse" / "native"
        if (
            "PULSE_SERVER" not in environment
            and not configured_socket.exists()
            and native_socket.exists()
        ):
            environment["PULSE_SERVER"] = f"unix:{native_socket}"
        return environment

    def snapshot(self) -> dict[str, Any]:
        stt_available, stt_reason = self.stt.available
        verification_available, verification_reason = self.stt.verification_available
        wake_available, wake_reason = self.wake.available
        microphone_live = (
            self.wake_audio_process is not None
            and self.wake_audio_process.returncode is None
            and 0 <= time.monotonic() - self.wake_last_feed_at < 5
        )
        diagnostics = {
            **self.diagnostics,
            "wake_worker_health": self.wake.health,
            "wake_speech_backup": self.speech_wake.snapshot(),
        }
        return {
            "echo_cancellation": self.echo.state,
            "speech_detector": {
                "engine": (
                    "silero_onnx"
                    if self.neural_vad.process is not None
                    and self.neural_vad.process.returncode is None
                    else "adaptive_energy"
                ),
                "local_only": True,
                "error": self.neural_vad.error,
            },
            "wake_available": wake_available,
            "wake_reason": wake_reason,
            "wake_enabled": self.wake_desired,
            "wake_paused": self.wake_paused,
            "privacy_mode": self.privacy_mode,
            "resource_suspended": self.resource_suspended,
            "wake_active": self.wake.listener_ready and microphone_live,
            "stt_available": stt_available,
            "stt_reason": stt_reason,
            "stt_provider": "whisper.cpp",
            "stt_model": self.stt.model_path.name if stt_available else "",
            "close_verification_available": verification_available,
            "close_verification_reason": verification_reason,
            "close_verification_model": (
                self.stt.verification_model_path.name if verification_available else ""
            ),
            "capture_available": self.capture_available,
            "tts_available": self.tts_available,
            "tts": self.tts.status,
            "microphone_active": self.capture_active or microphone_live,
            "speaking": self.speaking or self.speech_pending,
            "last_input_level": self.last_input_level,
            "diagnostics": diagnostics,
        }

    @staticmethod
    def _levels(
        pcm: bytes, points: int = 32, channels: int = 1, analysis_stride: int = 1
    ) -> dict[str, Any]:
        if not pcm:
            return {"rms": 0.0, "peak": 0.0, "clip_ratio": 0.0, "dc_offset": 0.0, "waveform": []}
        samples = array("h")
        samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
        if not samples:
            return {"rms": 0.0, "peak": 0.0, "clip_ratio": 0.0, "dc_offset": 0.0, "waveform": []}
        if os.sys.byteorder != "little":
            samples.byteswap()
        if channels > 1:
            samples = array("h", samples[::channels])
        stride_for_stats = max(1, analysis_stride)
        stats_samples = samples if stride_for_stats == 1 else samples[::stride_for_stats]
        peak = max(abs(sample) for sample in stats_samples) / 32768.0
        rms = (
            math.sqrt(sum(sample * sample for sample in stats_samples) / len(stats_samples))
            / 32768.0
        )
        clip_ratio = sum(abs(sample) >= 32760 for sample in stats_samples) / len(stats_samples)
        dc_offset = sum(stats_samples) / len(stats_samples) / 32768.0
        stride = max(1, len(samples) // points)
        waveform = [round(samples[index] / 32768.0, 4) for index in range(0, len(samples), stride)][
            :points
        ]
        return {
            "rms": round(rms, 4),
            "peak": round(peak, 4),
            "clip_ratio": round(clip_ratio, 4),
            "dc_offset": round(dc_offset, 4),
            "waveform": waveform,
        }

    async def _current_source(self) -> str:
        configured = str(self.config.get("microphone_source", "@DEFAULT_SOURCE@")).strip()
        if configured and configured != "@DEFAULT_SOURCE@":
            return await self.echo.ensure(configured)
        process = await asyncio.create_subprocess_exec(
            _platform_executable("/usr/bin/pactl"),
            "get-default-source",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=self._audio_env(),
        )
        stdout, _stderr = await process.communicate()
        source = (
            stdout.decode(errors="replace").strip()
            if process.returncode == 0
            else "@DEFAULT_SOURCE@"
        )
        return await self.echo.ensure(source)

    @property
    def _follows_default_source(self) -> bool:
        configured = str(self.config.get("microphone_source", "@DEFAULT_SOURCE@")).strip()
        return not configured or configured == "@DEFAULT_SOURCE@"

    async def _wait_for_default_source_change(self, active_source: str) -> str:
        """Return a new default source without interrupting an active utterance."""

        if not self._follows_default_source:
            return ""
        interval = max(
            0.1,
            min(30.0, float(self.config.get("wake", {}).get("source_poll_seconds", 1.0))),
        )
        while (
            self.wake_desired
            and not self.wake_paused
            and not self.privacy_mode
            and not self.resource_suspended
        ):
            await asyncio.sleep(interval)
            try:
                candidate = await self._current_source()
            except (OSError, RuntimeError):
                # A brief PipeWire failure shouldn't kill a working capture stream.
                # The supervisor handles recorders that actually exit.
                continue
            if not candidate or candidate == "@DEFAULT_SOURCE@" or candidate == active_source:
                continue
            if self.capture_active:
                # Let the current command finish before switching microphones.
                continue
            return candidate
        return ""

    def _wake_status(self, status: str, detail: str) -> None:
        self.diagnostics["wake_state"] = status
        if status == "ERROR":
            self.diagnostics["wake_error"] = detail
        self.bus.publish("wake.state_changed", "voice", {"state": status, "detail": detail})

    async def start(self) -> None:
        """Start optional ambient components after the core event loop is ready."""

        await self._refresh_wake_supervisor()

    async def prewarm(self) -> None:
        """Load reusable speech models without blocking core startup."""
        if self.privacy_mode:
            return
        await asyncio.gather(self.stt.prewarm(), self.tts.prewarm(), self.neural_vad.start())
        if self.config.get("partial_transcripts", False) or self.config.get("wake", {}).get(
            "speech_backup", False
        ):
            await self.preview_stt.prewarm()

    async def _refresh_wake_supervisor(self) -> None:
        should_run = (
            self.wake_desired
            and not self.wake_paused
            and not self.privacy_mode
            and not self.resource_suspended
        )
        if should_run and (self.wake_supervisor_task is None or self.wake_supervisor_task.done()):
            self.wake_supervisor_task = asyncio.create_task(self._wake_supervisor())
            return
        if not should_run:
            if self.wake_supervisor_task is not None:
                task = self.wake_supervisor_task
                self.wake_supervisor_task = None
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await self._stop_wake_runtime()
            self.wake_buffer.clear()
            self.wake_gate_buffer.clear()
            self.wake_gate_active = False
            self.wake_gate_silence_ms = 0.0
            self.wake_invalid_input_ms = 0.0
            self.diagnostics["wake_state"] = (
                "PRIVATE"
                if self.privacy_mode
                else "RESOURCE_SUSPENDED" if self.resource_suspended else "PAUSED"
            )

    async def _stop_process(self, process: asyncio.subprocess.Process | None) -> None:
        if process is None:
            return
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()

    async def _stop_wake_runtime(self) -> None:
        await self.speech_wake.close()
        self.wake_loud_unvoiced_ms = 0.0
        self.diagnostics["wake_input_quality"] = "UNTESTED"
        if self.wake_chime_task is not None and not self.wake_chime_task.done():
            self.wake_chime_task.cancel()
            await asyncio.gather(self.wake_chime_task, return_exceptions=True)
        self.wake_chime_task = None
        process = self.wake_audio_process
        self.wake_audio_process = None
        await self._stop_process(process)
        await self.wake.stop()
        await self.echo.close()

    async def _wake_supervisor(self) -> None:
        reconnect_seconds = max(
            0.5, float(self.config.get("wake", {}).get("reconnect_seconds", 2.0))
        )
        try:
            while self.wake_desired and not self.wake_paused and not self.privacy_mode:
                audio_task: asyncio.Task[None] | None = None
                source_task: asyncio.Task[str] | None = None
                try:
                    await self.wake.start(self._on_wake_detected, self._wake_status)
                    source = await self._current_source()
                    command = [str(part) for part in self.config["capture_command"]]
                    if source and not any(part.startswith("--device") for part in command):
                        command.append(f"--device={source}")
                    process = await asyncio.create_subprocess_exec(
                        *command,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        start_new_session=True,
                        env=self._audio_env(),
                    )
                    self.wake_filter.reset()
                    self.wake_noise_rms.clear()
                    self.wake_audio_process = process
                    self.diagnostics.update(
                        {"microphone": source, "wake_state": "ACTIVE", "wake_error": ""}
                    )
                    self.bus.publish(
                        "wake.listening_started",
                        "voice",
                        {
                            "source": source,
                            "engine": "sherpa-onnx",
                            "local_only": True,
                            "audio_retained": False,
                        },
                    )
                    audio_task = asyncio.create_task(self._wake_audio_loop(process))
                    worker_task = self.wake.reader_task
                    if self._follows_default_source:
                        source_task = asyncio.create_task(
                            self._wait_for_default_source_change(source)
                        )
                        waiters = {audio_task, source_task}
                        if worker_task is not None:
                            waiters.add(worker_task)
                        done, _pending = await asyncio.wait(
                            waiters,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if worker_task is not None and worker_task in done:
                            await worker_task
                            raise RuntimeError(
                                self.wake.last_error or "Wake worker event reader stopped"
                            )
                        if source_task in done:
                            replacement = source_task.result()
                            if replacement:
                                self.diagnostics.update(
                                    {"wake_state": "SWITCHING_SOURCE", "wake_error": ""}
                                )
                                self.bus.publish(
                                    "wake.source_changed",
                                    "voice",
                                    {
                                        "previous_source": source,
                                        "source": replacement,
                                        "automatic": True,
                                    },
                                )
                                await self._stop_process(process)
                                await asyncio.gather(audio_task, return_exceptions=True)
                                continue
                        await audio_task
                    else:
                        waiters = {audio_task}
                        if worker_task is not None:
                            waiters.add(worker_task)
                        done, _pending = await asyncio.wait(
                            waiters, return_when=asyncio.FIRST_COMPLETED
                        )
                        if worker_task is not None and worker_task in done:
                            await worker_task
                            raise RuntimeError(
                                self.wake.last_error or "Wake worker event reader stopped"
                            )
                        await audio_task
                    stderr = await process.stderr.read() if process.stderr else b""
                    detail = stderr.decode(errors="replace").strip()[:500]
                    raise RuntimeError(detail or "Microphone stream ended")
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    if self.capture_active and self.capture_origin == "ambient":
                        self.capture_error = str(error)
                        await self.stop_capture(self.capture_correlation_id)
                    self.diagnostics.update(
                        {"wake_state": "RECONNECTING", "wake_error": str(error)}
                    )
                    self.bus.publish(
                        "wake.reconnecting",
                        "voice",
                        {"error": str(error), "retry_seconds": reconnect_seconds},
                    )
                finally:
                    for task in (source_task, audio_task):
                        if task is not None and not task.done():
                            task.cancel()
                    await asyncio.gather(
                        *(task for task in (source_task, audio_task) if task is not None),
                        return_exceptions=True,
                    )
                    await self._stop_wake_runtime()
                    self.wake_buffer.clear()
                await asyncio.sleep(reconnect_seconds)
        except asyncio.CancelledError:
            raise
        finally:
            await self._stop_wake_runtime()

    async def _wake_audio_loop(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        last_emit = 0.0
        analysis_pcm = bytearray()
        while chunk := await asyncio.wait_for(process.stdout.read(1600), timeout=5):
            chunk = self.wake_filter.process(chunk)
            self.wake_buffer.append(chunk)
            now = time.monotonic()
            self.wake_last_feed_at = now
            self.wake.check_progress()
            if (
                self.capture_active
                and self.capture_origin == "ambient"
                and not self.capture_finishing
            ):
                if await self._analyze_capture_chunk(chunk):
                    self.capture_finishing = True
                    self.auto_stop_task = asyncio.create_task(
                        self._finish_automatic_capture(self.capture_correlation_id)
                    )
            analysis_pcm.extend(chunk)
            wake_settings = self.config.get("wake", {})
            invalid_dormant = (
                self.diagnostics.get("wake_state") == "INPUT_INVALID" and not self.capture_active
            )
            interval_key = "invalid_probe_ms" if invalid_dormant else "gate_analysis_ms"
            analysis_ms = float(wake_settings.get(interval_key, 250 if invalid_dormant else 100))
            if len(analysis_pcm) < max(1600, int(16000 * 2 * analysis_ms / 1000)):
                continue
            gate_pcm = bytes(analysis_pcm)
            analysis_pcm.clear()
            await self._speech_barge_in(gate_pcm)
            levels = self._levels(gate_pcm, analysis_stride=8)
            self.wake_noise_rms.append(float(levels["rms"]))
            if self.wake_test_armed_until >= now:
                self.wake_test_max_rms = max(self.wake_test_max_rms, float(levels["rms"]))
                self.wake_test_max_peak = max(self.wake_test_max_peak, float(levels["peak"]))
            frame_ms = len(gate_pcm) / (16000 * 2) * 1000
            invalid = (
                float(levels["clip_ratio"]) >= 0.03
                or abs(float(levels["dc_offset"]))
                >= float(wake_settings.get("gate_maximum_dc_offset", 0.5))
                or float(levels["rms"]) >= float(wake_settings.get("gate_maximum_rms", 0.18))
            )
            self.wake_invalid_input_ms = (
                self.wake_invalid_input_ms + frame_ms
                if invalid
                else max(0.0, self.wake_invalid_input_ms - frame_ms * 2)
            )
            invalid_hold = float(wake_settings.get("invalid_input_hold_ms", 1000))
            if self.wake_invalid_input_ms >= invalid_hold:
                self.diagnostics.update(
                    {
                        "wake_state": "INPUT_INVALID",
                        "input_rms": levels["rms"],
                        "input_peak": levels["peak"],
                        "input_clip_ratio": levels["clip_ratio"],
                        "input_dc_offset": levels["dc_offset"],
                    }
                )
                self.wake_gate_buffer.clear()
                self.wake_gate_active = False
                self.wake_gate_silence_ms = 0.0
            else:
                if self.diagnostics.get("wake_state") == "INPUT_INVALID":
                    self.diagnostics.update({"wake_state": "ACTIVE", "wake_error": ""})
                gate_rms = float(wake_settings.get("gate_minimum_rms", 0.008))
                gate_peak = float(wake_settings.get("gate_peak", 0.03))
                frame_active = (
                    float(levels["rms"]) >= gate_rms or float(levels["peak"]) >= gate_peak
                )
                if not self.wake_gate_active:
                    self.wake_gate_buffer.append(gate_pcm)
                    if frame_active:
                        self.wake_gate_active = True
                        for buffered in self.wake_gate_buffer:
                            await self.wake.feed(buffered)
                        self.wake_gate_buffer.clear()
                else:
                    await self.wake.feed(gate_pcm)
                    self.wake_gate_silence_ms = (
                        0.0 if frame_active else self.wake_gate_silence_ms + frame_ms
                    )
                    if self.wake_gate_silence_ms >= float(
                        wake_settings.get("gate_hangover_ms", 900)
                    ):
                        self.wake_gate_active = False
                        self.wake_gate_silence_ms = 0.0
            if not self.capture_active and now - last_emit >= 0.25:
                self.diagnostics.update(
                    {
                        "input_rms": levels["rms"],
                        "input_peak": levels["peak"],
                        "input_clip_ratio": levels["clip_ratio"],
                        "input_dc_offset": levels["dc_offset"],
                    }
                )
                # Show mic levels before the first wake too, so it doesn't look dead.
                self.bus.publish("voice.audio_level", "voice", {**levels, "ambient": True})
                last_emit = now
            if (
                bool(wake_settings.get("speech_backup", False))
                and self.wake_invalid_input_ms < invalid_hold
            ):
                await self.speech_wake.feed(gate_pcm)
                self.diagnostics["wake_speech_backup"] = self.speech_wake.snapshot()
                self._update_wake_input_quality(float(levels["rms"]), frame_ms)

    async def _speech_barge_in(self, pcm: bytes) -> None:
        # Ordinary speech can interrupt only an engaged speaking turn, with a
        # live echo reference. Without AEC, retain the explicit wake-word path.
        if (
            not self.echo.active
            or not self.speaking
            or self.capture_active
            or self.privacy_mode
            or not self.config.get("speech_barge_in", False)
        ):
            self._barge_speech_ms = 0.0
            return
        probability = await self.neural_vad.analyze(pcm, "carlos-barge-in")
        duration = len(pcm) / 32
        self._barge_speech_ms = (
            self._barge_speech_ms + duration
            if probability is not None and probability >= 0.85
            else 0.0
        )
        if self._barge_speech_ms >= 180:
            self._barge_speech_ms = 0.0
            self._wake_candidate_at = 0.0
            await self._on_wake_detected(
                {
                    "keyword": "CARLOS_INTERRUPT",
                    "source": "aec_speech",
                    "seed_pcm": b"".join(self.wake_buffer)[-16000:],
                    "seed_is_command": True,
                }
            )

    def _update_wake_input_quality(self, rms: float, frame_ms: float) -> None:
        probability = self.speech_wake.last_probability
        noisy = probability is not None and probability < 0.2 and rms >= 0.08
        self.wake_loud_unvoiced_ms = (
            self.wake_loud_unvoiced_ms + frame_ms
            if noisy
            else max(0.0, self.wake_loud_unvoiced_ms - frame_ms * 2)
        )
        quality = "LOUD_NON_SPEECH" if self.wake_loud_unvoiced_ms >= 8000 else "MONITORING"
        previous = self.diagnostics.get("wake_input_quality")
        self.diagnostics["wake_input_quality"] = quality
        if quality != previous and (quality == "LOUD_NON_SPEECH" or previous == "LOUD_NON_SPEECH"):
            self.bus.publish(
                "wake.input_quality_changed",
                "voice",
                {
                    "quality": quality,
                    "message": (
                        "The microphone is delivering sustained loud audio without clear speech. Check mic gain, placement and background sound."
                        if quality == "LOUD_NON_SPEECH"
                        else "The sustained loud-input warning has cleared."
                    ),
                },
            )

    def _speech_wake_allowed(self) -> bool:
        return (
            bool(self.config.get("wake", {}).get("speech_backup", False))
            and self.wake_desired
            and not self.wake_paused
            and not self.privacy_mode
            and not self.resource_suspended
            and self.state.current == CoreState.DORMANT
            and not self.capture_active
            and not self.capture_finishing
            and not self.speaking
            and not self.speech_pending
            and self.wake_test_armed_until < time.monotonic()
        )

    async def _audio_listing(self, kind: str) -> list[dict[str, Any]]:
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                _platform_executable("/usr/bin/pactl"),
                "-f",
                "json",
                "list",
                kind,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=self._audio_env(),
            )
            output, _ = await asyncio.wait_for(process.communicate(), 0.4)
            rows = json.loads(output)
            if process.returncode != 0 or not isinstance(rows, list):
                raise ValueError("Audio route listing unavailable")
            return [row for row in rows if isinstance(row, dict)]
        finally:
            if process is not None and process.returncode is None:
                await self._stop_process(process)

    @staticmethod
    def _private_audio_sink(sink: dict[str, Any]) -> bool:
        port = sink.get("active_port", "")
        if isinstance(port, dict):
            port = port.get("name", "")
        port = str(port).casefold()
        # Check the active port. A headset-capable card might be using speakers.
        if "speaker" in port or "hdmi" in port or "lineout" in port:
            return False
        return (
            "headphone" in port
            or "headset" in port
            or (
                not port
                and sink.get("properties", {}).get("device.form_factor") in ("headphone", "headset")
            )
        )

    async def _media_output_active(self) -> bool:
        """Whether external playback could enter the microphone from speakers."""
        if time.monotonic() - self._media_check_at < 2.0:
            return self._media_playing
        try:
            streams = [
                stream
                for stream in await self._audio_listing("sink-inputs")
                if (
                    not stream.get("corked", True)
                    and not stream.get("mute", False)
                    and stream.get("properties", {}).get("application.name") != "E.V."
                    and stream.get("properties", {}).get("media.name") != "E.V. speech"
                )
            ]
            self._media_playing = bool(streams)
            if streams:
                # Check where each stream actually plays. A movie in headphones
                # shouldn't block wake words or follow-ups.
                sinks = {
                    str(sink.get("index")): sink for sink in await self._audio_listing("sinks")
                }
                self._media_playing = any(
                    not (sink := sinks.get(str(stream.get("sink")), {})).get("mute", False)
                    and not self._private_audio_sink(sink)
                    for stream in streams
                )
        except (OSError, ValueError, asyncio.TimeoutError):
            # Unknown output? Keep the speaker guard. Dont guess it's headphones.
            pass
        self._media_check_at = time.monotonic()
        return self._media_playing

    async def _on_wake_detected(self, message: dict[str, Any]) -> None:
        if self.privacy_mode or self.wake_paused or not self.wake_desired:
            return
        detected_at = time.monotonic()
        detection_latency_ms = max(0.0, (detected_at - self.wake_last_feed_at) * 1000)
        correlation_id = uuid.uuid4().hex
        keyword = str(message.get("keyword", "E.V."))
        wake_settings = self.config.get("wake", {})
        cooldown = float(wake_settings.get("candidate_cooldown_seconds", 0.0))
        if detected_at - self._wake_candidate_at < cooldown:
            return
        self._wake_candidate_at = detected_at
        explicit_hey = keyword.upper().replace(" ", "_").startswith("HEY_")
        if (
            bool(wake_settings.get("media_requires_hey", False))
            and not bool(wake_settings.get("pause_media_on_wake", False))
            and not explicit_hey
            and not self.speaking
            and not self.speech_pending
            and await self._media_output_active()
        ):
            self.diagnostics.update(
                {"wake_state": "ACTIVE", "wake_guard": "MEDIA_REQUIRES_HEY", "wake_error": ""}
            )
            self.bus.publish(
                "wake.filtered",
                "voice",
                {"reason": "media_playing_use_hey_ev", "keyword": keyword},
                correlation_id,
            )
            return
        self.diagnostics["wake_guard"] = "READY"
        if message.get("source") != "local_speech_backup" and self.speech_wake.status != "DETECTED":
            await self.speech_wake.close()
        self.diagnostics.update(
            {
                "wake_state": "DETECTED",
                "wake_keyword": keyword,
                "wake_backend": str(message.get("source", "sherpa-onnx")),
                "wake_detection_latency_ms": round(detection_latency_ms, 3),
            }
        )
        self.bus.publish(
            "wake.detected",
            "voice",
            {"keyword": keyword, "latency_ms": round(detection_latency_ms, 3), "local_only": True},
            correlation_id,
            detection_latency_ms,
        )

        generating_speech = self.generating_speech
        if self.speaking or self.speech_pending or self.state.current == CoreState.SPEAKING:
            interrupted_at = time.monotonic()
            await self.stop_speaking("wake_word_barge_in", correlation_id)
            self.bus.publish(
                "voice.barge_in",
                "voice",
                {"interruption_latency_ms": round((time.monotonic() - interrupted_at) * 1000, 3)},
                correlation_id,
            )
        if generating_speech and self.interrupt_handler:
            await self.interrupt_handler()
        # A general-model failure must not disable the deterministic voice and
        # desktop routes. Bring the interaction state back before listening.
        if self.state.current == CoreState.OFFLINE and not self.capture_active:
            self.state.transition(
                CoreState.DORMANT, "Wake recovered the local interaction loop", correlation_id
            )
        if self.state.current == CoreState.TRANSCRIBING:
            task = self.auto_stop_task
            if task is not None and task is not asyncio.current_task() and not task.done():
                # Only the isolated speech-recognition phase is interrupted;
                # this cannot cancel or replay an already executing host tool.
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self.auto_stop_task = None
                self.diagnostics["stt_state"] = "CANCELLED"
                self.state.transition(
                    CoreState.DORMANT, "Wake interrupted the previous transcription", correlation_id
                )
                self.bus.publish(
                    "voice.transcription_interrupted",
                    "voice",
                    {"reason": "wake_word"},
                    correlation_id,
                )
        if (
            self.state.current in {CoreState.THINKING, CoreState.RETRIEVING_MEMORY}
            and self.interrupt_handler
        ):
            await self.interrupt_handler()
        if self.state.current != CoreState.DORMANT or self.capture_active:
            self.bus.publish(
                "wake.ignored",
                "voice",
                {"reason": f"core_{self.state.current.value.casefold()}"},
                correlation_id,
            )
            if self.wake.running:
                self.diagnostics["wake_state"] = "ACTIVE"
            return

        self.state.transition(CoreState.AWAKE, f"Wake word detected: {keyword}", correlation_id)
        if self.wake_test_armed_until >= detected_at:
            self.wake_test_armed_until = 0.0
            task = self.wake_test_task
            self.wake_test_task = None
            if task is not None and task is not asyncio.current_task():
                task.cancel()
            self.bus.publish(
                "wake.test_complete",
                "voice",
                {
                    "keyword": keyword,
                    "detected": True,
                    "max_input_rms": round(self.wake_test_max_rms, 6),
                    "max_input_peak": round(self.wake_test_max_peak, 6),
                },
                correlation_id,
            )
            self.state.transition(CoreState.DORMANT, "Wake-word test completed", correlation_id)
            self.diagnostics["wake_state"] = "ACTIVE"
            return

        seed = (
            message.get("seed_pcm")
            if message.get("source") == "local_speech_backup"
            else b"".join(self.wake_buffer)
        )
        seed = seed if isinstance(seed, bytes) else b""
        try:
            await self._activate_capture(
                correlation_id,
                "wake_command",
                "ambient",
                str(self.diagnostics.get("microphone", "")),
                seed,
                seed_is_command=bool(message.get("seed_is_command", False)),
            )
            wake_to_listening_ms = (time.monotonic() - detected_at) * 1000
            if bool(wake_settings.get("pause_media_on_wake", False)):
                await self.media_focus.pause()
                self._media_check_at = 0.0
                self.diagnostics["media_focus"] = "HELD"
                self.bus.publish(
                    "voice.media_held",
                    "voice",
                    {
                        "paused_players": len(self.media_focus.players),
                        "muted_streams": len(self.media_focus.streams),
                    },
                    correlation_id,
                )
                if self.media_focus_task is None or self.media_focus_task.done():
                    self.media_focus_task = asyncio.create_task(self._watch_media_focus())
            if bool(wake_settings.get("acknowledgement_chime", False)) and (
                self.wake_chime_task is None or self.wake_chime_task.done()
            ):
                self.wake_chime_task = asyncio.create_task(self._wake_chime())
        except Exception as error:
            await self.release_media_focus()
            self.capture_active = False
            self.capture_origin = ""
            self.capture_finishing = False
            self.capture_pcm.clear()
            self.diagnostics.update(
                {"capture_state": "IDLE", "wake_state": "ACTIVE", "wake_error": str(error)}
            )
            self.bus.publish(
                "system.error",
                "voice",
                {"message": f"Wake capture recovery: {error}"},
                correlation_id,
            )
            if self.state.current != CoreState.DORMANT:
                if self.state.current != CoreState.ERROR:
                    self.state.transition(
                        CoreState.ERROR, "Wake capture setup failed", correlation_id
                    )
                self.state.transition(
                    CoreState.DORMANT, "Recovered from wake capture setup failure", correlation_id
                )
            return
        self.diagnostics["wake_to_listening_ms"] = round(wake_to_listening_ms, 3)
        self.bus.publish(
            "wake.command_capture_started",
            "voice",
            {
                "wake_to_listening_ms": round(wake_to_listening_ms, 3),
                "rolling_buffer_bytes": len(seed),
            },
            correlation_id,
            wake_to_listening_ms,
        )

    async def set_wake_paused(self, paused: bool) -> dict[str, Any]:
        self.wake_paused = bool(paused)
        await self._refresh_wake_supervisor()
        self.bus.publish("wake.pause_changed", "voice", {"paused": self.wake_paused})
        return {"status": "completed", "wake_paused": self.wake_paused}

    async def _wake_chime(self) -> None:
        """A brief acknowledgement without invoking TTS or taking voice state."""
        process = None
        try:
            pcm = array("h")
            for frequency in (660, 880):
                length = 1654
                pcm.extend(
                    int(
                        1000
                        * math.sin(2 * math.pi * frequency * i / 22050)
                        * min(1, i / 130, (length - i) / 130)
                    )
                    for i in range(length)
                )
            if __import__("sys").byteorder != "little":
                pcm.byteswap()
            process = await asyncio.create_subprocess_exec(
                _platform_executable("/usr/bin/paplay"),
                "--raw",
                "--format=s16le",
                "--rate=22050",
                "--channels=1",
                "--client-name=E.V.",
                "--stream-name=E.V. wake",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=self._audio_env(),
            )
            await asyncio.wait_for(process.communicate(pcm.tobytes()), 2)
        except (OSError, asyncio.TimeoutError):
            self.bus.publish("voice.wake_chime_unavailable", "voice", {})
        finally:
            if process is not None and process.returncode is None:
                await self._stop_process(process)

    async def _watch_media_focus(self) -> None:
        idle_since = 0.0
        try:
            while self.media_focus.active:
                busy = (
                    self.capture_active
                    or self.capture_finishing
                    or self.speaking
                    or self.speech_pending
                    or self.state.current
                    not in {CoreState.DORMANT, CoreState.OFFLINE, CoreState.ERROR}
                )
                if busy:
                    idle_since = 0.0
                elif not idle_since:
                    idle_since = time.monotonic()
                elif time.monotonic() - idle_since >= 0.6:
                    break
                await asyncio.sleep(0.2)
        finally:
            await self._restore_media_focus()

    async def _restore_media_focus(self, *, resume: bool = True) -> None:
        for attempt in range(3):
            await self.media_focus.restore(resume=resume)
            if not self.media_focus.active:
                break
            await asyncio.sleep(0.2)
        self._media_check_at = 0.0
        self.diagnostics["media_focus"] = (
            "RESTORE_PENDING" if self.media_focus.active else "RELEASED"
        )
        self.bus.publish(
            "voice.media_released",
            "voice",
            {"restored": not self.media_focus.active, "resume": resume},
        )

    async def release_media_focus(self, *, resume: bool = True) -> None:
        if not resume:
            # An explicit playback command supersedes our automatic resume.
            self.media_focus.players.clear()
        task = self.media_focus_task
        self.media_focus_task = None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._restore_media_focus(resume=resume)

    async def set_privacy_mode(self, enabled: bool) -> dict[str, Any]:
        self.privacy_mode = bool(enabled)
        if self.privacy_mode:
            await self.abort_capture("privacy_mode")
            await self.stop_speaking("privacy_mode")
            await self.release_media_focus()
            if (
                self.auto_stop_task is not None
                and self.auto_stop_task is not asyncio.current_task()
                and not self.auto_stop_task.done()
            ):
                self.auto_stop_task.cancel()
                await asyncio.gather(self.auto_stop_task, return_exceptions=True)
            await self.stt.close()
            await self.preview_stt.close()
            await self.neural_vad.close()
        await self._refresh_wake_supervisor()
        self.bus.publish("voice.privacy_changed", "voice", {"enabled": self.privacy_mode})
        return {"status": "completed", "privacy_mode": self.privacy_mode}

    async def set_resource_mode(self, mode: str) -> None:
        suspended = mode == "CRITICAL"
        if suspended == self.resource_suspended:
            if mode == "CONSERVATION":
                self.wake_buffer.clear()
            return
        self.resource_suspended = suspended
        if suspended:
            await self.abort_capture("critical_memory_pressure")
        await self._refresh_wake_supervisor()
        self.bus.publish(
            "voice.resource_mode_applied",
            "voice",
            {"mode": mode, "wake_suspended": suspended},
        )

    def arm_wake_test(self, seconds: float = 15.0) -> dict[str, Any]:
        if not self.wake.running:
            raise RuntimeError("Wake detector is not active")
        timeout = max(3.0, min(30.0, float(seconds)))
        deadline = time.monotonic() + timeout
        self.wake_test_armed_until = deadline
        self.wake_test_max_rms = 0.0
        self.wake_test_max_peak = 0.0
        if self.wake_test_task is not None:
            self.wake_test_task.cancel()
        self.wake_test_task = asyncio.create_task(self._expire_wake_test(deadline, timeout))
        self.bus.publish(
            "wake.test_armed",
            "voice",
            {"timeout_seconds": timeout, "source": str(self.diagnostics.get("microphone", ""))},
        )
        return {"status": "armed", "timeout_seconds": timeout}

    async def _expire_wake_test(self, deadline: float, timeout: float) -> None:
        try:
            await asyncio.sleep(max(0.0, timeout))
            if self.wake_test_armed_until != deadline:
                return
            self.wake_test_armed_until = 0.0
            self.wake_test_task = None
            signal_detected = self.wake_test_max_rms >= float(
                self.config.get("wake", {}).get("gate_minimum_rms", 0.006)
            ) or self.wake_test_max_peak >= float(
                self.config.get("wake", {}).get("gate_peak", 0.02)
            )
            self.bus.publish(
                "wake.test_failed",
                "voice",
                {
                    "detected": False,
                    "reason": (
                        "keyword_not_recognized" if signal_detected else "no_microphone_signal"
                    ),
                    "timeout_seconds": timeout,
                    "source": str(self.diagnostics.get("microphone", "")),
                    "signal_detected": signal_detected,
                    "max_input_rms": round(self.wake_test_max_rms, 6),
                    "max_input_peak": round(self.wake_test_max_peak, 6),
                    "wake_state": str(self.diagnostics.get("wake_state", "")),
                },
            )
        except asyncio.CancelledError:
            raise

    async def start_capture(self, correlation_id: str, mode: str = "command") -> dict[str, Any]:
        if self.privacy_mode:
            raise RuntimeError("Microphone privacy mode is enabled")
        if self.capture_active:
            return {"status": "already_listening"}
        if not self.capture_available:
            raise RuntimeError("The configured microphone capture command is unavailable")
        if self.state.current == CoreState.OFFLINE:
            self.state.transition(CoreState.DORMANT, "Preparing microphone capture", correlation_id)
        if self.state.current != CoreState.DORMANT:
            raise RuntimeError(f"Cannot start listening while E.V. is {self.state.current.value}")
        source = await self._current_source()
        if self.wake_audio_process is not None and self.wake_audio_process.returncode is None:
            await self._activate_capture(correlation_id, mode, "ambient", source)
            return {
                "status": "listening",
                "sample_rate": 16000,
                "source": source,
                "mode": mode,
                "local_only": True,
            }
        command = [str(part) for part in self.config["capture_command"]]
        if source and not any(part.startswith("--device") for part in command):
            command.append(f"--device={source}")
        self.capture_process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            env=self._audio_env(),
        )
        self.capture_filter.reset()
        await self._activate_capture(correlation_id, mode, "manual", source)
        self.capture_task = asyncio.create_task(self._capture_loop(correlation_id))
        return {
            "status": "listening",
            "sample_rate": 16000,
            "source": source,
            "mode": mode,
            "local_only": True,
        }

    async def _activate_capture(
        self,
        correlation_id: str,
        mode: str,
        origin: str,
        source: str,
        seed: bytes = b"",
        *,
        seed_is_command: bool = False,
    ) -> None:
        self.capture_active = True
        self.capture_origin = origin
        self.capture_finishing = False
        self.capture_started = time.monotonic()
        self.capture_correlation_id = correlation_id
        self.capture_mode = mode
        self.capture_bytes = 0
        self.capture_seed_bytes = 0
        self.capture_error = ""
        self.capture_pcm.clear()
        self.capture_auto_reason = ""
        self.capture_speech_start_byte = None
        self.capture_speech_end_byte = None
        self._capture_last_emit = 0.0
        self._capture_last_activity = False
        self.capture_invalid_input_ms = 0.0
        ambient_samples = sorted(self.wake_noise_rms)
        ambient_rms = (
            ambient_samples[int((len(ambient_samples) - 1) * 0.2)] if ambient_samples else 0.0
        )
        self.vad.reset(ambient_rms)
        if mode == "full_test":
            self.pipeline_test_started = time.monotonic()
            self.pipeline_test = {
                "status": "ACTIVE",
                "stage": "MIC",
                "correlation_id": correlation_id,
                "timings_ms": {},
                "failure": "",
            }
            self.diagnostics["pipeline_test"] = dict(self.pipeline_test)
            self.bus.publish(
                "voice.full_test_started", "voice", {"requires_safe_tool": True}, correlation_id
            )
        self.diagnostics.update(
            {
                "microphone": source,
                "capture_state": "ACTIVE",
                "input_rms": 0.0,
                "input_peak": 0.0,
                "max_rms": 0.0,
                "max_peak": 0.0,
                "max_clip_ratio": 0.0,
                "max_abs_dc_offset": 0.0,
                "max_speech_probability": 0.0,
                "voice_activity": False,
                "noise_floor": 0.0,
                "capture_health": "MEASURING",
                "stt_state": "IDLE",
                "raw_transcript": "",
                "normalized_transcript": "",
                "stt_latency_ms": None,
                "detected_intent": "",
                "selected_tool": "",
                "tool_result": "",
                "close_verification_state": "IDLE",
                "close_verification_model": "",
                "close_verification_latency_ms": None,
                "permission_class": "",
            }
        )
        detail = {
            "wake_command": "Wake command capture active",
            "follow_up": "Follow-up listening active",
        }.get(mode, "Push-to-talk capture active")
        self.diagnostics["follow_up_state"] = "ACTIVE" if mode == "follow_up" else "IDLE"
        self.state.transition(CoreState.LISTENING, detail, correlation_id)
        self.bus.publish(
            "voice.listening_started",
            "voice",
            {
                "sample_rate": 16000,
                "channels": 1,
                "format": "s16le",
                "source": source,
                "mode": mode,
                "input_highpass_hz": (
                    self.wake_filter.cutoff_hz
                    if origin == "ambient"
                    else self.capture_filter.cutoff_hz
                ),
                "ambient_noise_rms": round(ambient_rms, 6),
                "local_only": True,
            },
            correlation_id,
        )
        if seed:
            if mode == "wake_command" and not seed_is_command:
                # Keep the wake phrase for transcription, but keep it out of command VAD.
                # Otherwise a pause after the name ends capture before the request starts.
                self.capture_pcm.extend(seed)
                self.capture_bytes += len(seed)
                self.capture_seed_bytes = len(seed)
            else:
                for offset in range(0, len(seed), 1600):
                    if self._consume_capture_chunk(
                        seed[offset : offset + 1600], 0.8 if seed_is_command else None
                    ):
                        self.capture_finishing = True
                        self.auto_stop_task = asyncio.create_task(
                            self._finish_automatic_capture(correlation_id)
                        )
                        break

    async def _analyze_capture_chunk(self, chunk: bytes) -> bool:
        correlation = self.capture_correlation_id
        probability = await self.neural_vad.analyze(chunk, correlation)
        if not self.capture_active or correlation != self.capture_correlation_id:
            return False
        self.diagnostics.update(
            {
                "vad_engine": "silero_onnx" if probability is not None else "adaptive_energy",
                "speech_probability": probability,
                "max_speech_probability": max(
                    float(self.diagnostics.get("max_speech_probability", 0.0)), probability or 0.0
                ),
                "vad_error": self.neural_vad.error,
            }
        )
        done = self._consume_capture_chunk(chunk, probability)
        if (
            not done
            and self.config.get("partial_transcripts", False)
            and self.capture_speech_start_byte is not None
        ):
            self.partial.feed(self.capture_pcm, correlation)
        return done

    def _partial_transcript(self, text, correlation, latency):
        if (
            self.capture_active
            and correlation == self.capture_correlation_id
            and not self.privacy_mode
        ):
            self.diagnostics["partial_transcript"] = text
            self.diagnostics["stt_partial_latency_ms"] = round(latency, 3)
            self.bus.publish(
                "voice.transcription_partial",
                "voice",
                {"text": text, "advisory": True},
                correlation,
                latency,
            )

    def _consume_capture_chunk(self, chunk: bytes, probability: float | None = None) -> bool:
        self.capture_bytes += len(chunk)
        self.capture_pcm.extend(chunk)
        now = time.monotonic()
        levels = self._levels(chunk)
        frame_ms = len(chunk) / (16000 * 2) * 1000
        activity = self.vad.update(
            float(levels["rms"]), float(levels["peak"]), frame_ms, probability
        )
        frame_invalid = (
            float(levels["clip_ratio"]) >= 0.03
            or abs(float(levels["dc_offset"]))
            >= float(self.config.get("wake", {}).get("gate_maximum_dc_offset", 0.5))
            or float(levels["rms"])
            >= float(self.config.get("wake", {}).get("gate_maximum_rms", 0.18))
        )
        self.capture_invalid_input_ms = (
            self.capture_invalid_input_ms + frame_ms
            if frame_invalid
            else max(0.0, self.capture_invalid_input_ms - frame_ms * 2)
        )
        self.diagnostics.update(
            {
                "input_rms": levels["rms"],
                "input_peak": levels["peak"],
                "input_clip_ratio": levels["clip_ratio"],
                "input_dc_offset": levels["dc_offset"],
                "max_rms": max(float(self.diagnostics["max_rms"]), float(levels["rms"])),
                "max_peak": max(float(self.diagnostics["max_peak"]), float(levels["peak"])),
                "max_clip_ratio": max(
                    float(self.diagnostics["max_clip_ratio"]), float(levels["clip_ratio"])
                ),
                "max_abs_dc_offset": max(
                    float(self.diagnostics["max_abs_dc_offset"]), abs(float(levels["dc_offset"]))
                ),
                "voice_activity": activity.active,
                "noise_floor": round(activity.noise_floor, 5),
            }
        )
        if now - self._capture_last_emit >= 0.05:
            self.last_input_level = {**levels, "vad": activity.as_dict()}
            self.bus.publish(
                "voice.audio_level", "voice", self.last_input_level, self.capture_correlation_id
            )
            self._capture_last_emit = now
        if (
            activity.active != self._capture_last_activity
            or activity.speech_started
            or activity.speech_ended
        ):
            self.bus.publish(
                "voice.activity_changed", "voice", activity.as_dict(), self.capture_correlation_id
            )
            self._capture_last_activity = activity.active
        invalid_hold = float(self.config.get("wake", {}).get("invalid_input_hold_ms", 1000))
        if self.capture_invalid_input_ms >= invalid_hold:
            self.capture_auto_reason = "invalid_input"
            return True
        if activity.speech_started and self.capture_speech_start_byte is None:
            bytes_per_ms = 16000 * 2 / 1000
            pre_roll_ms = float(self.config.get("vad", {}).get("pre_roll_ms", 450))
            leading_ms = activity.speech_ms + pre_roll_ms
            self.capture_speech_start_byte = max(
                0, self.capture_bytes - int(leading_ms * bytes_per_ms)
            )
        if activity.speech_ended and bool(self.config.get("vad", {}).get("automatic_end", True)):
            bytes_per_ms = 16000 * 2 / 1000
            tail_ms = float(self.config.get("vad", {}).get("stt_tail_ms", 300))
            removable_silence_ms = max(0.0, activity.silence_ms - tail_ms)
            self.capture_speech_end_byte = max(
                self.capture_speech_start_byte or 0,
                self.capture_bytes - int(removable_silence_ms * bytes_per_ms),
            )
            self.capture_auto_reason = "end_of_speech"
            return True
        if self.capture_mode == "wake_command" and self.capture_speech_start_byte is None:
            wait_seconds = max(
                1.0, float(self.config.get("wake", {}).get("command_wait_seconds", 6.0))
            )
            live_bytes = max(0, self.capture_bytes - self.capture_seed_bytes)
            if live_bytes >= int(wait_seconds * 16000 * 2):
                self.capture_auto_reason = "wake_command_timeout"
                self.bus.publish(
                    "voice.wake_command_timeout",
                    "voice",
                    {"wait_seconds": wait_seconds, "audio_retained": False},
                    self.capture_correlation_id,
                )
                return True
        limit_seconds = float(self.config.get("max_capture_seconds", 30))
        if self.capture_mode == "follow_up" and self.capture_speech_start_byte is None:
            wait_seconds = float(self.config.get("follow_up", {}).get("duration_seconds", 12.0))
            if self.capture_bytes >= int(wait_seconds * 16000 * 2):
                self.capture_auto_reason = "follow_up_timeout"
                return True
        maximum_bytes = int(limit_seconds * 16000 * 2)
        # The timeout is for starting a reply. Dont cut off someone mid-sentence.
        # Speech has its own length limit.
        utterance_start = self.capture_speech_start_byte or self.capture_seed_bytes
        if self.capture_bytes - utterance_start >= maximum_bytes:
            self.capture_auto_reason = "capture_limit"
            self.bus.publish(
                "voice.capture_limit_reached",
                "voice",
                {"max_seconds": self.config.get("max_capture_seconds", 30)},
                self.capture_correlation_id,
            )
            return True
        return False

    async def _capture_loop(self, correlation_id: str) -> None:
        assert self.capture_process is not None and self.capture_process.stdout is not None
        process = self.capture_process
        cancelled = False
        try:
            while chunk := await asyncio.wait_for(process.stdout.read(1600), timeout=5):
                chunk = self.capture_filter.process(chunk)
                if await self._analyze_capture_chunk(chunk):
                    self.capture_finishing = True
                    process.terminate()
                    break
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception as error:
            self.capture_error = str(error)
            self.bus.publish("system.error", "voice", {"message": str(error)}, correlation_id)
        finally:
            if (
                not cancelled
                and self.capture_active
                and self.capture_process is process
                and (self.capture_auto_reason or not self.capture_finishing)
            ):
                if not self.capture_auto_reason:
                    self.capture_error = self.capture_error or "Microphone stream ended or stalled"
                    self.capture_auto_reason = "microphone_stream_failed"
                self.capture_finishing = True
                self.auto_stop_task = asyncio.create_task(
                    self._finish_automatic_capture(correlation_id)
                )

    async def _finish_automatic_capture(self, correlation_id: str) -> None:
        try:
            result = await self.stop_capture(correlation_id)
            self.bus.publish("voice.capture_complete", "voice", result, correlation_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.capture_active = False
            self.capture_origin = ""
            self.capture_finishing = False
            self.capture_pcm.clear()
            self.diagnostics.update(
                {
                    "capture_state": "IDLE",
                    "stt_state": "ERROR",
                    "wake_state": "ACTIVE" if self.wake.running else "RECONNECTING",
                    "wake_error": str(error),
                }
            )
            self.bus.publish(
                "system.error",
                "voice",
                {"message": f"Voice interaction recovered: {error}"},
                correlation_id,
            )
            if self.state.current != CoreState.DORMANT:
                if self.state.current != CoreState.ERROR:
                    self.state.transition(
                        CoreState.ERROR, "Voice interaction failed", correlation_id
                    )
                self.state.transition(
                    CoreState.DORMANT, "Voice interaction recovered", correlation_id
                )

    def _capture_health(self, failure: str) -> str:
        if failure:
            return "DEVICE_ERROR"
        if self.capture_bytes == 0:
            return "SILENT"
        if self.capture_invalid_input_ms >= float(
            self.config.get("wake", {}).get("invalid_input_hold_ms", 1000)
        ):
            return "INPUT_INVALID"
        max_rms = float(self.diagnostics.get("max_rms", 0.0))
        max_peak = float(self.diagnostics.get("max_peak", 0.0))
        if max_peak >= 0.985:
            return "CLIPPING"
        if max_peak < 0.012 and max_rms < 0.0025:
            if (
                float(self.diagnostics.get("max_speech_probability", 0.0))
                >= self.vad.speech_probability
            ):
                return "TOO_QUIET"  # Neural-confirmed soft speech is not silence.
            return "SILENT"
        if max_rms < 0.012:
            return "TOO_QUIET"
        return "HEALTHY"

    async def stop_capture(self, correlation_id: str) -> dict[str, Any]:
        await self.partial.finish()
        async with self._capture_stop_lock:
            process = self.capture_process
            if not self.capture_active:
                return {"status": "not_listening"}
            origin = self.capture_origin
            self.capture_finishing = True
            correlation_id = self.capture_correlation_id or correlation_id
            if origin == "manual" and process is not None and process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            if origin == "manual" and process is not None:
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()
            if (
                origin == "manual"
                and self.capture_task is not None
                and self.capture_task is not asyncio.current_task()
            ):
                await asyncio.gather(self.capture_task, return_exceptions=True)
            duration = max(0.0, time.monotonic() - self.capture_started)
            byte_count = self.capture_bytes
            pcm = bytes(self.capture_pcm)
            speech_start = self.capture_speech_start_byte
            speech_end = self.capture_speech_end_byte
            stderr = (
                await process.stderr.read()
                if origin == "manual" and process is not None and process.stderr
                else b""
            )
            stderr_text = stderr.decode(errors="replace").strip()[:300]
            return_code = process.returncode if process is not None else 0
            failure = self.capture_error or (stderr_text if return_code not in {0, -15} else "")
            health = self._capture_health(failure)
            mode = self.capture_mode
            automatic_reason = self.capture_auto_reason
            end_silence_ms = self.vad.silence_ms
            self.capture_active = False
            self.capture_origin = ""
            self.capture_finishing = False
            self.capture_process = None
            self.capture_task = None
            self.capture_pcm.clear()
            self.capture_seed_bytes = 0
            self.capture_error = ""
            self.capture_auto_reason = ""
            self.capture_speech_start_byte = None
            self.capture_speech_end_byte = None
            self.diagnostics.update(
                {
                    "capture_state": "ERROR" if failure else "IDLE",
                    "last_capture_duration_ms": round(duration * 1000, 3),
                    "capture_health": health,
                    "voice_activity": False,
                    "follow_up_state": "IDLE",
                }
            )
            if self.diagnostics.get("wake_state") == "DETECTED" and self.wake.running:
                self.diagnostics["wake_state"] = "ACTIVE"
            stopped = {
                "duration_ms": round(duration * 1000, 3),
                "captured_bytes": byte_count,
                "capture_health": health,
                "automatic_reason": automatic_reason,
                "audio_retained": False,
            }
            self.bus.publish("voice.listening_stopped", "voice", stopped, correlation_id)
            if mode == "full_test":
                self.pipeline_test["timings_ms"]["audio_capture"] = round(duration * 1000, 3)
                self.pipeline_test["timings_ms"]["end_of_speech"] = round(end_silence_ms, 3)
                self.pipeline_test["stage"] = "VAD"
                self.diagnostics["pipeline_test"] = dict(self.pipeline_test)
            if failure:
                self._fail_pipeline_test("MIC", failure, correlation_id)
                self.bus.publish(
                    "system.error",
                    "voice",
                    {"message": failure, "stage": "capture"},
                    correlation_id,
                )
                if self.state.current == CoreState.LISTENING:
                    self.state.transition(
                        CoreState.DORMANT, "Microphone capture failed", correlation_id
                    )
                return {"status": "capture_failed", **stopped, "error": failure}
            if mode == "microphone_test":
                if self.state.current == CoreState.LISTENING:
                    self.state.transition(
                        CoreState.DORMANT, f"Microphone test: {health}", correlation_id
                    )
                return {
                    "status": "microphone_test_complete",
                    **stopped,
                    "diagnostics": dict(self.diagnostics),
                }
            if health == "INPUT_INVALID":
                self.bus.publish(
                    "voice.invalid_input",
                    "voice",
                    {
                        "capture_health": health,
                        "max_clip_ratio": self.diagnostics["max_clip_ratio"],
                        "max_abs_dc_offset": self.diagnostics["max_abs_dc_offset"],
                    },
                    correlation_id,
                )
                self._fail_pipeline_test(
                    "MIC",
                    "The selected microphone audio is distorted or continuously too loud",
                    correlation_id,
                )
                if self.state.current == CoreState.LISTENING:
                    self.state.transition(
                        CoreState.DORMANT, "Selected microphone input is invalid", correlation_id
                    )
                return {"status": "invalid_input", **stopped}
            if health == "SILENT" or (
                speech_start is None and mode in {"wake_command", "follow_up"}
            ):
                self._fail_pipeline_test("VAD", "No usable speech was detected", correlation_id)
                self.bus.publish(
                    "voice.no_speech", "voice", {"capture_health": health}, correlation_id
                )
                if self.state.current == CoreState.LISTENING:
                    self.state.transition(
                        CoreState.DORMANT, "No audible speech detected", correlation_id
                    )
                return {"status": "no_speech", **stopped}

            # Trim the wait before speech so Whisper doesn't invent words in silence.
            # Keep some audio at both edges. With no VAD boundary, keep the full clip.
            if speech_start is not None:
                bounded_end = min(len(pcm), speech_end if speech_end is not None else len(pcm))
                bounded_start = min(speech_start, bounded_end)
                stt_pcm = pcm[bounded_start:bounded_end]
            else:
                stt_pcm = pcm
            stt_duration_ms = len(stt_pcm) / (16000 * 2) * 1000
            self.diagnostics["stt_audio_duration_ms"] = round(stt_duration_ms, 3)

            stt_available, stt_reason = self.stt.available
            if not stt_available:
                self._fail_pipeline_test("STT", stt_reason, correlation_id)
                self.diagnostics["stt_state"] = "UNAVAILABLE"
                self.bus.publish(
                    "voice.transcription_unavailable",
                    "voice",
                    {"reason": stt_reason, "audio_retained": False},
                    correlation_id,
                )
                if self.state.current == CoreState.LISTENING:
                    self.state.transition(
                        CoreState.DORMANT, "Speech-to-text unavailable", correlation_id
                    )
                return {"status": "transcription_unavailable", **stopped, "reason": stt_reason}

            self.state.transition(
                CoreState.TRANSCRIBING, "Transcribing captured speech locally", correlation_id
            )
            self.diagnostics["stt_state"] = "TRANSCRIBING"
            self.bus.publish(
                "voice.transcription_started",
                "voice",
                {"engine": "whisper.cpp", "captured_bytes": byte_count},
                correlation_id,
            )
            try:
                transcript = await self.stt.transcribe(stt_pcm)
            except Exception as error:
                self._fail_pipeline_test("STT", str(error), correlation_id)
                self.diagnostics["stt_state"] = "ERROR"
                self.bus.publish(
                    "voice.transcription_failed",
                    "voice",
                    {"stage": "stt", "error": str(error)},
                    correlation_id,
                )
                self.state.transition(
                    CoreState.ERROR, "Local speech transcription failed", correlation_id
                )
                self.state.transition(
                    CoreState.DORMANT, "Recovered from transcription failure", correlation_id
                )
                return {"status": "transcription_failed", **stopped, "error": str(error)}

            normalized = normalize_transcript(transcript.raw)
            interpretation = interpret_spoken_command(
                normalized, media_active=bool(self.media_focus.players or self.media_focus.streams)
            )
            normalized = interpretation.text
            self.diagnostics.update(
                {
                    "stt_state": "SUCCESS",
                    "raw_transcript": transcript.raw,
                    "normalized_transcript": normalized,
                    "stt_latency_ms": round(transcript.latency_ms, 3),
                }
            )
            if mode == "full_test":
                self.pipeline_test["timings_ms"]["stt"] = round(transcript.latency_ms, 3)
                self.pipeline_test["stage"] = "INTENT"
                self.diagnostics["pipeline_test"] = dict(self.pipeline_test)
            transcription = {
                "raw_transcript": transcript.raw,
                "normalized_transcript": normalized,
                "engine": transcript.engine,
                "model": transcript.model,
                "latency_ms": round(transcript.latency_ms, 3),
                "audio_retained": False,
                "speech_repair": interpretation.repair,
            }
            self.bus.publish(
                "voice.transcription_complete",
                "voice",
                transcription,
                correlation_id,
                transcript.latency_ms,
            )

            # Closing the wrong app would suck. Check explicit close requests with
            # a second Whisper model and require the same target. No recording is kept.
            primary_close_targets = extract_close_targets(normalized)
            if primary_close_targets:
                verification_available, verification_reason = self.stt.verification_available
                if not verification_available:
                    del stt_pcm
                    del pcm
                    return self._reject_unverified_close(
                        stopped,
                        transcription,
                        correlation_id,
                        primary_close_targets,
                        (),
                        verification_reason,
                    )
                self.diagnostics["close_verification_state"] = "VERIFYING"
                self.bus.publish(
                    "voice.close_verification_started",
                    "voice",
                    {"primary_targets": list(primary_close_targets), "audio_retained": False},
                    correlation_id,
                )
                try:
                    verification = await self.stt.transcribe_verification(stt_pcm)
                except Exception as error:
                    del stt_pcm
                    del pcm
                    return self._reject_unverified_close(
                        stopped,
                        transcription,
                        correlation_id,
                        primary_close_targets,
                        (),
                        str(error),
                    )
                verification_normalized = normalize_transcript(verification.raw)
                verification_targets = extract_close_targets(verification_normalized)
                self.diagnostics.update(
                    {
                        "close_verification_model": verification.model,
                        "close_verification_latency_ms": round(verification.latency_ms, 3),
                    }
                )
                if verification_targets != primary_close_targets:
                    del stt_pcm
                    del pcm
                    return self._reject_unverified_close(
                        stopped,
                        transcription,
                        correlation_id,
                        primary_close_targets,
                        verification_targets,
                        "The independent transcripts named different close targets",
                    )
                self.diagnostics["close_verification_state"] = "VERIFIED"
                self.bus.publish(
                    "voice.close_verification_complete",
                    "voice",
                    {
                        "verified": True,
                        "primary_targets": list(primary_close_targets),
                        "verification_targets": list(verification_targets),
                        "model": verification.model,
                        "latency_ms": round(verification.latency_ms, 3),
                        "audio_retained": False,
                    },
                    correlation_id,
                    verification.latency_ms,
                )
                transcription.update(
                    {
                        "close_targets_verified": list(primary_close_targets),
                        "verification_model": verification.model,
                        "verification_latency_ms": round(verification.latency_ms, 3),
                    }
                )
            else:
                self.diagnostics["close_verification_state"] = "NOT_REQUIRED"
            del stt_pcm
            del pcm

            if not normalized:
                self._fail_pipeline_test(
                    "STT", "The transcript was empty after normalization", correlation_id
                )
                self.state.transition(
                    CoreState.DORMANT,
                    "No command remained after conservative normalization",
                    correlation_id,
                )
                return {"status": "empty_transcript", **stopped, **transcription}
            if is_conversation_stop(normalized):
                self.bus.publish(
                    "voice.conversation_ended",
                    "voice",
                    {"phrase": normalized, "reason": "spoken_stop"},
                    correlation_id,
                )
                self.state.transition(
                    CoreState.DORMANT, "Conversation window closed", correlation_id
                )
                return {"status": "conversation_ended", **stopped, **transcription}
            if mode == "transcription_test" or self.command_handler is None:
                self.state.transition(
                    CoreState.DORMANT, "Transcription test completed", correlation_id
                )
                return {"status": "transcription_complete", **stopped, **transcription}

            if interpretation.clarification:
                self.state.transition(
                    CoreState.DORMANT, "Clarifying a short media request", correlation_id
                )
                command = {
                    "status": "completed",
                    "correlation_id": correlation_id,
                    "response": interpretation.clarification,
                    "clarification": True,
                }
            else:
                command = await self.command_handler(normalized, correlation_id)
            cognition = (
                command.get("cognition", {}) if isinstance(command.get("cognition"), dict) else {}
            )
            selected_tool = ""
            permission_class = ""
            for event in reversed(self.bus.history(80)):
                if (
                    event.get("correlation_id") != correlation_id
                    or event.get("type") != "tool.requested"
                ):
                    continue
                payload = event.get("payload", {})
                selected_tool = str(payload.get("tool", ""))
                permission_class = str(payload.get("permission", ""))
                break
            confirmation = (
                command.get("confirmation", {})
                if isinstance(command.get("confirmation"), dict)
                else {}
            )
            if confirmation:
                selected_tool = str(confirmation.get("tool", selected_tool))
                permission_class = str(confirmation.get("permission", permission_class))
            self.diagnostics.update(
                {
                    "detected_intent": str(cognition.get("interpreted_task", "")),
                    "selected_tool": selected_tool,
                    "permission_class": permission_class,
                    "tool_result": str(command.get("status", "")),
                }
            )
            if mode == "full_test":
                related = [
                    event
                    for event in self.bus.history(120)
                    if event.get("correlation_id") == correlation_id
                ]
                ai_timings = [
                    float(event.get("duration_ms", 0.0))
                    for event in related
                    if event.get("type") == "ai.request_complete"
                ]
                tool_timings = [
                    float(event.get("duration_ms", 0.0))
                    for event in related
                    if event.get("type") == "tool.completed"
                ]
                self.pipeline_test["timings_ms"].update(
                    {
                        "intent_classification": round(ai_timings[0], 3) if ai_timings else None,
                        "tool_execution": round(sum(tool_timings), 3) if tool_timings else None,
                        "response_generation": (
                            round(sum(ai_timings[1:]), 3) if len(ai_timings) > 1 else None
                        ),
                    }
                )
                self.pipeline_test.update({"tool": selected_tool, "permission": permission_class})
                if not selected_tool:
                    self._fail_pipeline_test(
                        "TOOL", "The spoken request did not select a tool", correlation_id
                    )
                elif permission_class != "SAFE":
                    self._fail_pipeline_test(
                        "TOOL", "Full-pipeline tests only execute SAFE tools", correlation_id
                    )
                elif command.get("status") != "completed":
                    self._fail_pipeline_test(
                        "TOOL", f"Tool pipeline returned {command.get('status')}", correlation_id
                    )
                else:
                    self.pipeline_test.update({"status": "ACTIVE", "stage": "TTS"})
                    self.diagnostics["pipeline_test"] = dict(self.pipeline_test)
            if self.response_handler is not None and (
                mode != "full_test" or self.pipeline_test.get("stage") == "TTS"
            ):
                self.response_handler(command)
            self.bus.publish(
                "voice.command_complete",
                "voice",
                {
                    "status": command.get("status"),
                    "response": command.get("response", ""),
                    "intent": self.diagnostics["detected_intent"],
                    "tool": selected_tool,
                    "permission": permission_class,
                },
                correlation_id,
            )
            return {
                "status": "voice_command_complete",
                **stopped,
                **transcription,
                "command": command,
            }

    def _reject_unverified_close(
        self,
        stopped: dict[str, Any],
        transcription: dict[str, Any],
        correlation_id: str,
        primary_targets: tuple[str, ...],
        verification_targets: tuple[str, ...],
        reason: str,
    ) -> dict[str, Any]:
        """Fail closed while still giving an audible, non-interactive result."""

        self.diagnostics.update(
            {
                "close_verification_state": "BLOCKED",
                "detected_intent": "Verify the spoken application name before an automatic close.",
                "selected_tool": "",
                "permission_class": "SENSITIVE",
                "tool_result": "blocked_unverified_speech",
            }
        )
        self.bus.publish(
            "voice.close_verification_complete",
            "voice",
            {
                "verified": False,
                "primary_targets": list(primary_targets),
                "verification_targets": list(verification_targets),
                "reason": reason[:300],
                "audio_retained": False,
            },
            correlation_id,
        )
        if self.state.current != CoreState.DORMANT:
            self.state.transition(
                CoreState.DORMANT, "Unverified spoken close was blocked", correlation_id
            )
        response = (
            "I heard two different app names, so I did not close anything."
            if verification_targets
            else "I could not independently verify the app name, so I did not close anything."
        )
        command = {
            "status": "completed",
            "correlation_id": correlation_id,
            "response": response,
            "safety_blocked": True,
            "cognition": {
                "interpreted_task": "Verify the spoken application name before an automatic close.",
                "plan": [
                    "Compare independent local speech transcripts",
                    "Do not close on disagreement",
                ],
            },
        }
        if self.response_handler is not None:
            self.response_handler(command)
        self.bus.publish(
            "voice.command_complete",
            "voice",
            {
                "status": "blocked_unverified_speech",
                "response": response,
                "intent": self.diagnostics["detected_intent"],
                "tool": "",
                "permission": "SENSITIVE",
            },
            correlation_id,
        )
        return {
            "status": "voice_command_complete",
            **stopped,
            **transcription,
            "close_verification": {
                "verified": False,
                "primary_targets": list(primary_targets),
                "verification_targets": list(verification_targets),
            },
            "command": command,
        }

    def _fail_pipeline_test(self, stage: str, failure: str, correlation_id: str) -> None:
        if not self.pipeline_test or self.capture_mode != "full_test":
            return
        self.pipeline_test.update({"status": "FAILED", "stage": stage, "failure": failure})
        if self.pipeline_test_started:
            self.pipeline_test["timings_ms"]["total"] = round(
                (time.monotonic() - self.pipeline_test_started) * 1000, 3
            )
        self.diagnostics["pipeline_test"] = dict(self.pipeline_test)
        self.bus.publish(
            "voice.full_test_failed", "voice", dict(self.pipeline_test), correlation_id
        )

    async def abort_capture(self, reason: str) -> dict[str, Any]:
        """Discard an active microphone interaction without transcribing it."""

        await self.partial.finish()
        async with self._capture_stop_lock:
            if not self.capture_active:
                self.capture_pcm.clear()
                return {"status": "not_listening"}
            process = self.capture_process
            origin = self.capture_origin
            self.capture_active = False
            self.capture_finishing = False
            self.capture_origin = ""
            if origin == "manual":
                await self._stop_process(process)
            if self.capture_task is not None and self.capture_task is not asyncio.current_task():
                self.capture_task.cancel()
                await asyncio.gather(self.capture_task, return_exceptions=True)
            discarded_bytes = len(self.capture_pcm)
            self.capture_process = None
            self.capture_task = None
            self.capture_pcm.clear()
            self.capture_bytes = 0
            self.capture_speech_start_byte = None
            self.capture_speech_end_byte = None
            self.diagnostics.update(
                {"capture_state": "IDLE", "voice_activity": False, "follow_up_state": "IDLE"}
            )
            self.bus.publish(
                "voice.capture_aborted",
                "voice",
                {"reason": reason, "discarded_bytes": discarded_bytes, "audio_retained": False},
                self.capture_correlation_id,
            )
            if self.state.current == CoreState.LISTENING:
                self.state.transition(
                    CoreState.DORMANT, f"Capture stopped: {reason}", self.capture_correlation_id
                )
            return {"status": "cancelled", "reason": reason, "audio_retained": False}

    @asynccontextmanager
    async def _speech_cleanup(self, correlation_id: str):
        """Cover synthesis and player startup as well as playback failures."""
        try:
            yield
        except BaseException as error:
            cancelled = isinstance(error, asyncio.CancelledError)
            self.diagnostics["tts_state"] = "CANCELLED" if cancelled else "FAILED"
            self.bus.publish(
                "tts.interaction_failed",
                "voice",
                {"cancelled": cancelled, "error": str(error)},
                correlation_id,
            )
            if self.state.current == CoreState.SPEAKING:
                self.state.transition(
                    CoreState.DORMANT, "Speech ended; ready for another request", correlation_id
                )
            raise
        finally:
            player = self.tts_process
            self.tts_process = None
            self.speaking = False
            self.speech_pending = False
            self.generating_speech = False
            await self._stop_process(player)

    async def speak(
        self, text: str, correlation_id: str, allow_follow_up: bool = False, continuation=None
    ) -> dict[str, Any]:
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ValueError("speech text must be 1-2000 characters")
        if not self.tts_available:
            raise RuntimeError(f"TTS is unavailable: {self.tts.available[1]}")
        async with self._tts_lock, self._speech_cleanup(correlation_id):
            if self.state.current == CoreState.OFFLINE:
                self.state.transition(CoreState.DORMANT, "Preparing speech output", correlation_id)
            if self.state.current != CoreState.DORMANT and not (
                continuation is not None and self.state.current == CoreState.THINKING
            ):
                raise RuntimeError(f"Cannot speak while E.V. is {self.state.current.value}")
            started = time.monotonic()
            self.tts_cancel_reason = ""
            self.speech_pending = True
            self.generating_speech = continuation is not None
            chunks = (
                speech_chunks(text) if self.config.get("streaming_tts", True) else [text.strip()]
            )
            self.diagnostics.update({"tts_state": "LOADING", "tts_startup_latency_ms": None})
            try:
                synthesized = await asyncio.wait_for(self.tts.synthesize(chunks[0]), timeout=60)
            except Exception as error:
                self.speech_pending = False
                if self.pipeline_test.get("correlation_id") == correlation_id:
                    self._fail_pipeline_test("TTS", str(error), correlation_id)
                raise
            if self.tts_cancel_reason:
                self.speech_pending = False
                self.diagnostics["tts_state"] = "CANCELLED"
                return {
                    "status": "cancelled",
                    "engine": synthesized.engine,
                    "reason": self.tts_cancel_reason,
                }
            channels = synthesized.channels
            rate = synthesized.sample_rate
            width = synthesized.sample_width
            pcm = synthesized.pcm
            if width != 2 or rate <= 0 or channels <= 0 or not pcm:
                raise RuntimeError("Synthesized audio has an unsupported or empty PCM format")
            output_device = str(
                self.config.get("tts", {}).get("output_device", "@DEFAULT_SINK@")
            ).strip()
            player_command = [
                _platform_executable("/usr/bin/paplay"),
                "--raw",
                "--format=s16le",
                f"--rate={rate}",
                f"--channels={channels}",
                "--latency-msec=150",
                "--client-name=E.V.",
                "--stream-name=E.V. speech",
            ]
            if output_device and output_device != "@DEFAULT_SINK@":
                player_command.append(f"--device={output_device}")
            player = await asyncio.create_subprocess_exec(
                *player_command,
                stdin=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._audio_env(),
            )
            self.tts_process = player
            # A wake word can land while paplay starts. Dont let that late player
            # switch Carlos back to SPEAKING when he's already listening.
            if self.tts_cancel_reason:
                await self._stop_process(player)
                self.tts_process = None
                self.speech_pending = False
                self.diagnostics["tts_state"] = "CANCELLED"
                return {
                    "status": "cancelled",
                    "engine": synthesized.engine,
                    "reason": self.tts_cancel_reason,
                }
            assert player.stdin is not None
            self.speaking = True
            self.state.transition(CoreState.SPEAKING, "Speaking response", correlation_id)
            startup_ms = (time.monotonic() - started) * 1000
            self.diagnostics.update(
                {"tts_state": "ACTIVE", "tts_startup_latency_ms": round(startup_ms, 3)}
            )
            self.bus.publish(
                "tts.started",
                "voice",
                {
                    "engine": synthesized.engine,
                    "voice": synthesized.voice,
                    "sample_rate": rate,
                    "synthesis_latency_ms": round(synthesized.latency_ms, 3),
                    "startup_latency_ms": round(startup_ms, 3),
                },
                correlation_id,
                startup_ms,
            )
            frame_bytes = width * channels
            bytes_per_second = rate * frame_bytes
            chunk_bytes = max(frame_bytes, int(rate * 0.1) * frame_bytes)
            outcome = "completed"

            async def report_levels() -> None:
                playback_started = time.monotonic()
                while not self.tts_cancel_reason:
                    offset = int((time.monotonic() - playback_started) * rate) * frame_bytes
                    if offset >= len(pcm):
                        return
                    self.bus.publish(
                        "tts.audio_level",
                        "voice",
                        self._levels(pcm[offset : offset + chunk_bytes], channels=channels),
                        correlation_id,
                    )
                    await asyncio.sleep(0.1)

            async def stop_player() -> None:
                if not player.stdin.is_closing():
                    player.stdin.close()
                if player.returncode is None:
                    try:
                        player.terminate()
                    except ProcessLookupError:
                        pass
                try:
                    await asyncio.wait_for(player.wait(), timeout=1)
                except TimeoutError:
                    try:
                        player.kill()
                    except ProcessLookupError:
                        pass
                    await player.wait()

            meter_task = asyncio.create_task(report_levels())
            try:
                # Feed playback right away. Waiting on telemetry here starves the audio.
                if len(chunks) == 1 and continuation is None:
                    _stdout, stderr = await asyncio.wait_for(
                        player.communicate(pcm),
                        timeout=max(20.0, len(pcm) / bytes_per_second + 15.0),
                    )
                else:
                    async with asyncio.timeout(120):
                        player.stdin.write(pcm)
                        await player.stdin.drain()

                        async def remaining():
                            for sentence in chunks[1:]:
                                yield sentence
                            if continuation is not None:
                                async for sentence in continuation:
                                    for part in speech_chunks(sentence):
                                        yield part

                        async for sentence in remaining():
                            if self.tts_cancel_reason:
                                break
                            following = await self.tts.synthesize(sentence)
                            if self.tts_cancel_reason:
                                break
                            if (
                                following.sample_rate,
                                following.channels,
                                following.sample_width,
                            ) != (rate, channels, width):
                                raise RuntimeError("TTS changed format within a speech stream")
                            pcm += following.pcm
                            player.stdin.write(following.pcm)
                            await player.stdin.drain()
                        player.stdin.close()
                        await player.wait()
                        stderr = await player.stderr.read() if player.stderr else b""
                if self.tts_cancel_reason:
                    outcome = "cancelled"
                elif player.returncode != 0:
                    raise RuntimeError(
                        f"paplay failed: {(stderr or b'').decode(errors='replace')[:300]}"
                    )
            except asyncio.CancelledError:
                outcome = "cancelled"
                await stop_player()
                raise
            except Exception:
                outcome = "cancelled" if self.tts_cancel_reason else "failed"
                await stop_player()
                if outcome == "failed":
                    raise
            finally:
                meter_task.cancel()
                await asyncio.gather(meter_task, return_exceptions=True)
                self.tts_process = None
                self.speaking = False
                self.speech_pending = False
                self.generating_speech = False
                self.diagnostics["tts_state"] = outcome.upper()
                duration_ms = (time.monotonic() - started) * 1000
                self.bus.publish(
                    f"tts.{outcome}",
                    "voice",
                    {"engine": synthesized.engine, "voice": synthesized.voice},
                    correlation_id,
                    duration_ms,
                )
                if self.state.current == CoreState.SPEAKING:
                    detail = {
                        "completed": "Speech completed",
                        "cancelled": "Speech cancelled",
                        "failed": "Speech failed",
                    }[outcome]
                    self.state.transition(CoreState.DORMANT, detail, correlation_id)
            result = {
                "status": outcome,
                "engine": synthesized.engine,
                "voice": synthesized.voice,
                "duration_ms": round(duration_ms, 3),
                "startup_latency_ms": round(startup_ms, 3),
            }
            is_pipeline_test = self.pipeline_test.get("correlation_id") == correlation_id
            if is_pipeline_test:
                self.pipeline_test["timings_ms"].update(
                    {
                        "tts_startup": round(startup_ms, 3),
                        "total": round((time.monotonic() - self.pipeline_test_started) * 1000, 3),
                    }
                )
                if outcome == "completed":
                    self.pipeline_test.update(
                        {"status": "SUCCESS", "stage": "COMPLETE", "failure": ""}
                    )
                    self.bus.publish(
                        "voice.full_test_complete",
                        "voice",
                        dict(self.pipeline_test),
                        correlation_id,
                    )
                else:
                    self._fail_pipeline_test("TTS", f"Playback was {outcome}", correlation_id)
                self.diagnostics["pipeline_test"] = dict(self.pipeline_test)
            follow_up = self.config.get("follow_up", {})
            if (
                outcome == "completed"
                and allow_follow_up
                and not is_pipeline_test
                and bool(follow_up.get("enabled", True))
                and not self.privacy_mode
                and self.state.current == CoreState.DORMANT
            ):
                await self._start_follow_up()
            return result

    async def _start_follow_up(self) -> None:
        if (
            bool(self.config.get("wake", {}).get("media_requires_hey", True))
            and await self._media_output_active()
        ):
            # A movie's next line is not permission for a follow-up command.
            self.diagnostics["follow_up_state"] = "WAITING_FOR_HEY"
            self.diagnostics["wake_guard"] = "MEDIA_REQUIRES_HEY"
            self.bus.publish(
                "voice.follow_up_deferred",
                "voice",
                {
                    "reason": "media_playing",
                    "detail": "Say Hey E.V. while other audio is playing",
                },
            )
            return
        await self.start_capture(uuid.uuid4().hex, "follow_up")

    async def stop_speaking(
        self, reason: str = "user_request", correlation_id: str | None = None
    ) -> dict[str, Any]:
        started = time.monotonic()
        if not self.speaking and not self.speech_pending and self.tts_process is None:
            return {"status": "not_speaking"}
        self.tts_cancel_reason = reason
        process = self.tts_process
        if process is not None and process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=1)
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        latency_ms = (time.monotonic() - started) * 1000
        self.diagnostics.update(
            {"tts_state": "CANCELLED", "barge_in_latency_ms": round(latency_ms, 3)}
        )
        self.bus.publish(
            "tts.interrupted",
            "voice",
            {"reason": reason, "latency_ms": round(latency_ms, 3)},
            correlation_id,
            latency_ms,
        )
        if self.state.current == CoreState.SPEAKING:
            self.state.transition(
                CoreState.DORMANT, f"Speech interrupted: {reason}", correlation_id
            )
        return {"status": "cancelled", "reason": reason, "latency_ms": round(latency_ms, 3)}

    async def end_conversation(
        self, reason: str = "spoken_stop", correlation_id: str | None = None
    ) -> dict[str, Any]:
        """Immediately stop output/listening and close the current follow-up window."""

        speech = await self.stop_speaking(reason, correlation_id)
        capture = await self.abort_capture(reason)
        self.diagnostics["follow_up_state"] = "IDLE"
        await self.release_media_focus()
        self.bus.publish(
            "voice.conversation_ended",
            "voice",
            {
                "reason": reason,
                "speech_status": speech["status"],
                "capture_status": capture["status"],
            },
            correlation_id,
        )
        return {"status": "conversation_ended", "reason": reason}

    async def close(self) -> None:
        self.wake_desired = False
        await self.release_media_focus()
        if self.wake_test_task is not None:
            task = self.wake_test_task
            self.wake_test_task = None
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.wake_test_armed_until = 0.0
        if self.wake_supervisor_task is not None:
            task = self.wake_supervisor_task
            self.wake_supervisor_task = None
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._stop_wake_runtime()
        self.wake_buffer.clear()
        if self.auto_stop_task is not None and self.auto_stop_task is not asyncio.current_task():
            self.auto_stop_task.cancel()
            await asyncio.gather(self.auto_stop_task, return_exceptions=True)
            self.auto_stop_task = None
        if self.tts_process is not None and self.tts_process.returncode is None:
            try:
                self.tts_process.terminate()
            except ProcessLookupError:
                pass
            await asyncio.gather(self.tts_process.wait(), return_exceptions=True)
        if self.capture_process is not None:
            if self.capture_process.returncode is None:
                try:
                    self.capture_process.terminate()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(self.capture_process.wait(), timeout=1)
            except TimeoutError:
                try:
                    self.capture_process.kill()
                except ProcessLookupError:
                    pass
                await self.capture_process.wait()
        if self.capture_task is not None:
            self.capture_task.cancel()
            await asyncio.gather(self.capture_task, return_exceptions=True)
        self.capture_process = None
        self.capture_task = None
        self.capture_active = False
        self.capture_origin = ""
        self.capture_pcm.clear()
        await self.stt.close()
        await self.partial.finish()
        await self.preview_stt.close()
        await self.tts.close()
        await self.neural_vad.close()
