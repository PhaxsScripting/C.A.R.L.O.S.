from __future__ import annotations

from ev.platform import executable as _platform_executable

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

DetectionHandler = Callable[[dict[str, Any]], Awaitable[None]]
StatusHandler = Callable[[str, str], None]


class WakeWordWorker:
    """Supervise the isolated Sherpa-ONNX keyword-spotting process."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.process: asyncio.subprocess.Process | None = None
        self.reader_task: asyncio.Task[None] | None = None
        self.stderr_task: asyncio.Task[None] | None = None
        self.on_detection: DetectionHandler | None = None
        self.on_status: StatusHandler | None = None
        self.last_error = ""
        self.fed_bytes = 0
        self.processed_bytes = 0
        self.last_progress_at = 0.0
        self.started_at = 0.0
        self.backlog_since = 0.0
        self.callback_active = False

    @property
    def health(self) -> dict[str, Any]:
        backlog_ms = max(0, self.fed_bytes - self.processed_bytes) / 32
        return {
            "running": self.running,
            "listener_ready": self.listener_ready,
            "fed_bytes": self.fed_bytes,
            "processed_bytes": self.processed_bytes,
            "backlog_ms": round(backlog_ms),
            "last_progress_age_ms": (
                round((time.monotonic() - self.last_progress_at) * 1000)
                if self.last_progress_at
                else None
            ),
            "backlog_kind": "unacknowledged_audio_upper_bound",
            "progress_interval_ms": 1000,
            "callback_active": self.callback_active,
        }

    def check_progress(self) -> None:
        # Energy gating deliberately sends no silent frames. Check unprocessed
        # audio, not time since last heartbeat, so silence cannot cause resets.
        now = time.monotonic()
        stalled = self.fed_bytes - self.processed_bytes > 3 * 32000 and not self.callback_active
        if stalled and not self.backlog_since:
            self.backlog_since = now
        elif not stalled:
            self.backlog_since = 0.0
        if self.backlog_since and now - self.backlog_since > 4:
            raise RuntimeError(
                "Wake recognizer stopped consuming microphone audio; restarting its worker"
            )

    @property
    def python(self) -> Path:
        return Path(str(self.config.get("python", ""))).expanduser()

    @property
    def model_dir(self) -> Path:
        return Path(str(self.config.get("model_dir", ""))).expanduser()

    @property
    def keywords(self) -> Path:
        return Path(str(self.config.get("keywords", ""))).expanduser()

    def _files(self) -> dict[str, Path]:
        root = self.model_dir
        return {
            "tokens": root / str(self.config.get("tokens", "tokens.txt")),
            "encoder": root
            / str(self.config.get("encoder", "encoder-epoch-13-avg-2-chunk-16-left-64.int8.onnx")),
            "decoder": root
            / str(self.config.get("decoder", "decoder-epoch-13-avg-2-chunk-16-left-64.onnx")),
            "joiner": root
            / str(self.config.get("joiner", "joiner-epoch-13-avg-2-chunk-16-left-64.int8.onnx")),
        }

    @property
    def available(self) -> tuple[bool, str]:
        if not self.python.is_file() or not os.access(self.python, os.X_OK):
            return False, f"Wake Python runtime is unavailable: {self.python}"
        missing = [
            str(path) for path in [*self._files().values(), self.keywords] if not path.is_file()
        ]
        if missing:
            return False, f"Wake model file is unavailable: {missing[0]}"
        return True, "Sherpa-ONNX E.V. wake model is ready"

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    @property
    def listener_ready(self) -> bool:
        return self.running and self.reader_task is not None and not self.reader_task.done()

    def _notify(self, state: str, detail: str) -> None:
        if self.on_status is not None:
            self.on_status(state, detail)

    async def start(self, on_detection: DetectionHandler, on_status: StatusHandler) -> None:
        if self.running:
            return
        available, reason = self.available
        if not available:
            raise RuntimeError(reason)
        self.on_detection = on_detection
        self.on_status = on_status
        self.fed_bytes = self.processed_bytes = 0
        self.last_progress_at = self.backlog_since = 0.0
        self.started_at = time.monotonic()
        self.last_error = ""
        files = self._files()
        command = [
            _platform_executable("/usr/bin/nice"),
            "-n",
            str(int(self.config.get("nice", 15))),
            str(self.python),
            "-m",
            "ev.voice.wake_worker",
            "--tokens",
            str(files["tokens"]),
            "--encoder",
            str(files["encoder"]),
            "--decoder",
            str(files["decoder"]),
            "--joiner",
            str(files["joiner"]),
            "--keywords",
            str(self.keywords),
            "--threads",
            str(int(self.config.get("threads", 1))),
            "--score",
            str(float(self.config.get("score", 1.5))),
            "--threshold",
            str(float(self.config.get("threshold", 0.18))),
        ]
        self._notify("LOADING", "Loading local wake model")
        spawning = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        )
        try:
            self.process = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            # Capture ownership even if cancellation arrives as the OS creates
            # the child. The supervisor cannot clean up a handle we lost.
            self.process = await spawning
            await self.stop()
            raise
        try:
            assert self.process.stdout is not None
            # Model initialization may emit enough stderr to fill its pipe;
            # drain it while waiting for readiness, not only afterwards.
            self.stderr_task = asyncio.create_task(self._read_stderr())
            line = await asyncio.wait_for(
                self.process.stdout.readline(),
                timeout=float(self.config.get("load_timeout_seconds", 15)),
            )
            if not line:
                raise RuntimeError("Wake worker exited before becoming ready")
            message = json.loads(line)
            if not isinstance(message, dict) or message.get("type") != "ready":
                raise RuntimeError("Wake worker failed during initialization")
            self._notify("ACTIVE", "Listening locally for E.V.")
            self.reader_task = asyncio.create_task(self._read_events())
        except TimeoutError:
            await self.stop()
            raise RuntimeError("Wake model loading timed out") from None
        except BaseException:
            # Includes malformed readiness, cancellation and callback errors.
            # A failed start must never leave a nominally running worker that
            # causes the next start to return without an event reader.
            await self.stop()
            raise

    async def _read_events(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if message.get("type") == "detected" and self.on_detection is not None:
                    try:
                        self.callback_active = True
                        await self.on_detection(message)
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        # One state race must not kill the only reader while the
                        # worker and microphone continue to look healthy.
                        self.last_error = f"Wake callback failed: {type(error).__name__}: {error}"
                        self._notify("ERROR", self.last_error)
                    finally:
                        self.callback_active = False
                elif message.get("type") == "progress":
                    processed = message.get("processed_bytes")
                    if (
                        type(processed) is int
                        and self.processed_bytes < processed <= self.fed_bytes
                    ):
                        self.processed_bytes = max(self.processed_bytes, processed)
                        self.last_progress_at = time.monotonic()
                elif message.get("type") == "error":
                    self.last_error = str(message.get("error", "Unknown wake worker error"))
                    self._notify("ERROR", self.last_error)
            if self.process is not None:
                self.last_error = self.last_error or "Wake worker event stream ended"
                self._notify("ERROR", self.last_error)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.last_error = f"Wake worker protocol error: {error}"
            self._notify("ERROR", self.last_error)

    async def _read_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        try:
            while chunk := await self.process.stderr.read(4096):
                # Diagnostics need not contain newlines. readline() can fail
                # its size limit and abandon a full pipe during model loading.
                text = chunk.decode(errors="replace").strip()
                if text:
                    self.last_error = text[-500:]
                await asyncio.sleep(0)  # Do not monopolize the loop on a burst.
        except asyncio.CancelledError:
            raise

    async def feed(self, pcm: bytes) -> None:
        if not self.running or self.process is None or self.process.stdin is None:
            raise RuntimeError("Wake worker is not running")
        self.process.stdin.write(pcm)
        self.fed_bytes += len(pcm)
        if self.process.stdin.transport.get_write_buffer_size() > 65536:
            await asyncio.wait_for(self.process.stdin.drain(), timeout=3)

    async def stop(self) -> None:
        process = self.process
        self.process = None
        current = asyncio.current_task()
        for task in (self.reader_task, self.stderr_task):
            if task is not None and task is not current:
                task.cancel()
        await asyncio.gather(
            *(
                task
                for task in (self.reader_task, self.stderr_task)
                if task is not None and task is not current
            ),
            return_exceptions=True,
        )
        self.reader_task = None
        self.stderr_task = None
        if process is None:
            return
        if process.stdin is not None and not process.stdin.is_closing():
            process.stdin.close()
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
