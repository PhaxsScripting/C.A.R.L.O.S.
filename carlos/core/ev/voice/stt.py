from __future__ import annotations

from ev.platform import executable as _platform_executable

import asyncio
import json
import os
import signal
import stat
import tempfile
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

_SERVER_OWNERSHIP_VERSION = 1
_SERVER_OWNERSHIP_NAME = "whisper-server-owner.json"


@dataclass(frozen=True, slots=True)
class _ProcessIdentity:
    pid: int
    uid: int
    exe: str
    cmdline: tuple[str, ...]
    start_time: int

    def to_json(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "uid": self.uid,
            "exe": self.exe,
            "cmdline": list(self.cmdline),
            "start_time": self.start_time,
        }

    @classmethod
    def from_json(cls, value: Any) -> _ProcessIdentity:
        if not isinstance(value, dict):
            raise ValueError("process identity must be an object")
        pid = value.get("pid")
        uid = value.get("uid")
        executable = value.get("exe")
        command_line = value.get("cmdline")
        start_time = value.get("start_time")
        if (
            type(pid) is not int
            or pid <= 0
            or type(uid) is not int
            or uid < 0
            or not isinstance(executable, str)
            or not executable
            or not isinstance(command_line, list)
            or not command_line
            or not all(isinstance(argument, str) for argument in command_line)
            or type(start_time) is not int
            or start_time <= 0
        ):
            raise ValueError("process identity has invalid fields")
        return cls(pid, uid, executable, tuple(command_line), start_time)


@dataclass(frozen=True, slots=True)
class _ServerOwnership:
    boot_id: str
    owner: _ProcessIdentity
    server: _ProcessIdentity

    def to_json(self) -> dict[str, Any]:
        return {
            "version": _SERVER_OWNERSHIP_VERSION,
            "boot_id": self.boot_id,
            "owner": self.owner.to_json(),
            "server": self.server.to_json(),
        }

    @classmethod
    def from_json(cls, value: Any) -> _ServerOwnership:
        if not isinstance(value, dict) or value.get("version") != _SERVER_OWNERSHIP_VERSION:
            raise ValueError("unsupported Whisper server ownership record")
        boot_id = value.get("boot_id")
        if not isinstance(boot_id, str) or not boot_id:
            raise ValueError("ownership record has no boot ID")
        return cls(
            boot_id=boot_id,
            owner=_ProcessIdentity.from_json(value.get("owner")),
            server=_ProcessIdentity.from_json(value.get("server")),
        )


def _process_stat(pid: int) -> tuple[str, int] | None:
    """Read Linux process state and immutable start tick without parsing comm."""

    from ev.platform import IS_FREEBSD

    if IS_FREEBSD:
        import psutil

        try:
            p = psutil.Process(pid)
            return (
                "Z" if p.status() == psutil.STATUS_ZOMBIE else "R",
                int(p.create_time() * 1_000_000),
            )
        except psutil.Error:
            return None
    try:
        raw = (Path("/proc") / str(pid) / "stat").read_bytes()
        closing_parenthesis = raw.rfind(b")")
        if closing_parenthesis < 0:
            return None
        fields = raw[closing_parenthesis + 2 :].split()
        if len(fields) <= 19:
            return None
        return os.fsdecode(fields[0]), int(fields[19])
    except (OSError, ValueError):
        return None


def _process_start_time(pid: int) -> int | None:
    process_stat = _process_stat(pid)
    return process_stat[1] if process_stat is not None else None


def _same_process_alive(identity: _ProcessIdentity) -> bool:
    process_stat = _process_stat(identity.pid)
    return (
        process_stat is not None
        and process_stat[0] not in {"Z", "X", "x"}
        and process_stat[1] == identity.start_time
    )


def _process_identity(pid: int) -> _ProcessIdentity | None:
    """Return one stable `/proc` snapshot, or no identity on any ambiguity."""

    from ev.platform import IS_FREEBSD

    if IS_FREEBSD:
        from ev.platform.system import process_identity

        p = process_identity(pid)
        return (
            _ProcessIdentity(
                p["pid"], p["uid"], p["exe"], tuple(p["cmdline"]), int(p["start_time"])
            )
            if p
            else None
        )
    process_dir = Path("/proc") / str(pid)
    try:
        start_time = _process_start_time(pid)
        if start_time is None:
            return None
        uid = process_dir.stat().st_uid
        executable = os.path.realpath(os.readlink(process_dir / "exe"))
        raw_command_line = (process_dir / "cmdline").read_bytes()
        command_line = tuple(
            os.fsdecode(argument)
            for argument in raw_command_line.rstrip(b"\0").split(b"\0")
            if argument
        )
        if not command_line or _process_start_time(pid) != start_time:
            return None
        return _ProcessIdentity(pid, uid, executable, command_line, start_time)
    except OSError:
        return None


def _current_boot_id() -> str:
    from ev.platform import IS_FREEBSD

    if IS_FREEBSD:
        from ev.platform.system import boot_id

        return boot_id()
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return ""


def _loopback_listener_inodes(pid: int, port: int) -> dict[int, int] | None:
    """Map exact 127.0.0.1 listeners to inode and socket UID in a PID's netns."""

    expected_address = f"0100007F:{port:04X}"
    try:
        lines = (Path("/proc") / str(pid) / "net/tcp").read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    listeners: dict[int, int] = {}
    try:
        for line in lines[1:]:
            fields = line.split()
            if len(fields) <= 9 or fields[1].upper() != expected_address or fields[3] != "0A":
                continue
            uid = int(fields[7])
            inode = int(fields[9])
            if inode <= 0:
                return None
            listeners[inode] = uid
    except (IndexError, ValueError):
        return None
    return listeners


def _process_socket_inodes(pid: int) -> set[int] | None:
    """Read socket inodes from one exact process; never scan unrelated PIDs."""

    descriptors = Path("/proc") / str(pid) / "fd"
    try:
        entries = tuple(descriptors.iterdir())
    except OSError:
        return None
    sockets: set[int] = set()
    for entry in entries:
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        if not target.startswith("socket:[") or not target.endswith("]"):
            continue
        try:
            sockets.add(int(target[8:-1]))
        except ValueError:
            return None
    return sockets


@dataclass(slots=True)
class Transcript:
    raw: str
    engine: str
    model: str
    latency_ms: float


class WhisperCppAdapter:
    """whisper.cpp with a restartable local server and CLI fallback."""

    def __init__(self, config: dict[str, Any], runtime_dir: Path) -> None:
        self.config = config
        self.runtime_dir = runtime_dir
        self._server_process: asyncio.subprocess.Process | None = None
        self._server_ownership: _ServerOwnership | None = None
        self._server_lock = asyncio.Lock()

    @property
    def binary(self) -> Path:
        return Path(str(self.config.get("binary", ""))).expanduser()

    @property
    def model_path(self) -> Path:
        return Path(str(self.config.get("model", ""))).expanduser()

    @property
    def server_binary(self) -> Path:
        configured = str(self.config.get("server_binary", "")).strip()
        return (
            Path(configured).expanduser() if configured else self.binary.with_name("whisper-server")
        )

    @property
    def verification_model_path(self) -> Path:
        return Path(str(self.config.get("verification_model", ""))).expanduser()

    @property
    def available(self) -> tuple[bool, str]:
        if not self.binary.is_file() or not os.access(self.binary, os.X_OK):
            return False, f"whisper.cpp binary is unavailable: {self.binary}"
        if not self.model_path.is_file():
            return False, f"Whisper model is unavailable: {self.model_path}"
        return True, "whisper.cpp and the configured model are ready"

    @property
    def verification_available(self) -> tuple[bool, str]:
        available, reason = self.available
        if not available:
            return False, reason
        configured = str(self.config.get("verification_model", "")).strip()
        if not configured:
            return False, "No independent close-command verification model is configured"
        if not self.verification_model_path.is_file():
            return (
                False,
                f"Close-command verification model is unavailable: {self.verification_model_path}",
            )
        if self.verification_model_path == self.model_path:
            return False, "Close-command verification must use an independent model"
        return True, "Independent close-command speech verification is ready"

    async def transcribe(self, pcm: bytes, sample_rate: int = 16000) -> Transcript:
        available, reason = self.available
        if not available:
            raise RuntimeError(reason)
        return await self._transcribe(
            pcm,
            sample_rate,
            model_path=self.model_path,
            prompt=str(self.config.get("prompt", "")),
            threads=int(self.config.get("threads", 2)),
            nice=int(self.config.get("nice", 15)),
            timeout_seconds=float(self.config.get("timeout_seconds", 45)),
            engine="whisper.cpp",
        )

    async def prewarm(self) -> None:
        if not bool(self.config.get("persistent_server", True)) or not self.server_binary.is_file():
            return
        async with self._server_lock:
            await self._ensure_server()

    @property
    def _ownership_path(self) -> Path:
        return self.runtime_dir / _SERVER_OWNERSHIP_NAME

    def _server_command(self) -> list[str]:
        return [
            str(self.server_binary),
            "-m",
            str(self.model_path),
            "-t",
            str(max(1, min(4, int(self.config.get("threads", 4))))),
            "-l",
            str(self.config.get("language", "en")),
            "-nt",
            "-sns",
            "--host",
            "127.0.0.1",
            "--port",
            str(int(self.config.get("server_port", 18082))),
            "--tmp-dir",
            str(self.runtime_dir),
        ]

    def _new_ownership(self, server_pid: int, server_command: list[str]) -> _ServerOwnership:
        owner = _process_identity(os.getpid())
        server_start_time = _process_start_time(server_pid)
        boot_id = _current_boot_id()
        try:
            executable = str(self.server_binary.resolve(strict=True))
        except OSError as error:
            raise RuntimeError(
                "Whisper server executable identity could not be resolved"
            ) from error
        if owner is None or owner.uid != os.getuid() or server_start_time is None or not boot_id:
            raise RuntimeError("Whisper server ownership could not be recorded safely")
        return _ServerOwnership(
            boot_id=boot_id,
            owner=owner,
            server=_ProcessIdentity(
                pid=server_pid,
                uid=os.getuid(),
                exe=executable,
                cmdline=tuple(server_command),
                start_time=server_start_time,
            ),
        )

    def _read_ownership(self) -> _ServerOwnership | None:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self._ownership_path, flags)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise RuntimeError(
                "Whisper server ownership record could not be opened safely"
            ) from error
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
                or metadata.st_size <= 0
                or metadata.st_size > 65_536
            ):
                raise RuntimeError("Whisper server ownership record is not a private regular file")
            raw = os.read(descriptor, 65_537)
            if len(raw) > 65_536:
                raise RuntimeError("Whisper server ownership record is too large")
        finally:
            os.close(descriptor)
        try:
            record = _ServerOwnership.from_json(json.loads(raw))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as error:
            raise RuntimeError("Whisper server ownership record is invalid") from error
        if record.owner.uid != os.getuid() or record.server.uid != os.getuid():
            raise RuntimeError("Whisper server ownership UID does not match this user")
        return record

    def _write_ownership(self, record: _ServerOwnership) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{_SERVER_OWNERSHIP_NAME}.",
            dir=self.runtime_dir,
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(record.to_json(), handle, separators=(",", ":"), sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._ownership_path)
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _discard_ownership(self, record: _ServerOwnership) -> None:
        try:
            current = self._read_ownership()
        except RuntimeError:
            return
        if current != record:
            return
        try:
            self._ownership_path.unlink()
        except FileNotFoundError:
            pass

    def _ownership_matches_configuration(self, record: _ServerOwnership) -> bool:
        try:
            executable = str(self.server_binary.resolve(strict=True))
        except OSError:
            return False
        return (
            record.server.uid == os.getuid()
            and record.server.exe == executable
            and record.server.cmdline == tuple(self._server_command())
        )

    async def _wait_for_recorded_exit(self, identity: _ProcessIdentity, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while _same_process_alive(identity):
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.05)
        return True

    async def _reclaim_stale_server(self) -> bool:
        """Stop only a precisely recorded server whose owning core is gone."""

        record = self._read_ownership()
        if record is None:
            return False
        boot_id = _current_boot_id()
        if not boot_id:
            raise RuntimeError("Whisper server boot identity could not be verified")
        if record.boot_id != boot_id:
            self._discard_ownership(record)
            return False
        if not _same_process_alive(record.server):
            self._discard_ownership(record)
            return False

        pidfd_open = getattr(os, "pidfd_open", None)
        pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
        if pidfd_open is None or pidfd_send_signal is None:
            raise RuntimeError("Safe Whisper server recovery requires Linux pidfd support")
        try:
            process_descriptor = pidfd_open(record.server.pid, 0)
        except ProcessLookupError:
            self._discard_ownership(record)
            return False
        except OSError as error:
            raise RuntimeError(
                "Whisper server process handle could not be opened safely"
            ) from error

        try:
            actual_server = _process_identity(record.server.pid)
            if actual_server != record.server:
                raise RuntimeError(
                    "Recorded Whisper server identity no longer matches the live process"
                )
            if not self._ownership_matches_configuration(record):
                raise RuntimeError("Recorded Whisper server does not match the configured server")

            if _same_process_alive(record.owner):
                actual_owner = _process_identity(record.owner.pid)
                if actual_owner == record.owner:
                    raise RuntimeError("Recorded Whisper server is still owned by a live E.V. core")
                raise RuntimeError("Recorded Whisper server owner identity is ambiguous")

            try:
                pidfd_send_signal(process_descriptor, signal.SIGTERM, None, 0)
            except ProcessLookupError:
                pass
            except OSError as error:
                raise RuntimeError("Recorded Whisper server could not be terminated") from error

            timeout = max(0.1, min(10.0, float(self.config.get("server_stop_timeout_seconds", 2))))
            if not await self._wait_for_recorded_exit(record.server, timeout):
                if _process_identity(record.server.pid) != record.server:
                    raise RuntimeError("Whisper server identity changed before forced shutdown")
                try:
                    pidfd_send_signal(process_descriptor, signal.SIGKILL, None, 0)
                except ProcessLookupError:
                    pass
                except OSError as error:
                    raise RuntimeError("Recorded Whisper server could not be killed") from error
                if not await self._wait_for_recorded_exit(record.server, timeout):
                    raise RuntimeError("Recorded Whisper server did not exit")
        finally:
            os.close(process_descriptor)

        self._discard_ownership(record)
        return True

    def _owns_loopback_listener(self, record: _ServerOwnership) -> bool:
        if (
            _current_boot_id() != record.boot_id
            or _process_identity(record.server.pid) != record.server
            or not self._ownership_matches_configuration(record)
        ):
            return False
        from ev.platform import IS_FREEBSD

        if IS_FREEBSD:
            from ev.platform.freebsd_sockets import owns_listener

            return (
                owns_listener(
                    record.server.pid, "127.0.0.1", int(self.config.get("server_port", 18082))
                )
                and _process_identity(record.server.pid) == record.server
            )
        listeners = _loopback_listener_inodes(
            record.server.pid,
            int(self.config.get("server_port", 18082)),
        )
        sockets = _process_socket_inodes(record.server.pid)
        if listeners is None or sockets is None or len(listeners) != 1:
            return False
        inode, uid = next(iter(listeners.items()))
        return (
            uid == record.server.uid
            and inode in sockets
            and _process_identity(record.server.pid) == record.server
        )

    async def _port_ready(self) -> bool:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", int(self.config.get("server_port", 18082))),
                timeout=0.3,
            )
            del reader
            writer.close()
            await writer.wait_closed()
            return True
        except (OSError, TimeoutError):
            return False

    async def _managed_server_ready(self, record: _ServerOwnership) -> bool:
        process = self._server_process
        if (
            process is None
            or process.returncode is not None
            or process.pid != record.server.pid
            or not self._owns_loopback_listener(record)
        ):
            return False
        if not await self._port_ready():
            return False
        return self._owns_loopback_listener(record)

    async def _stop_managed_server(self) -> None:
        process = self._server_process
        record = self._server_ownership
        self._server_process = None
        self._server_ownership = None
        if process is None:
            return
        stopped = process.returncode is not None
        if not stopped:
            try:
                process.terminate()
            except ProcessLookupError:
                stopped = True
            if not stopped:
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                    stopped = True
                except TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        stopped = True
                    if not stopped:
                        await process.wait()
                        stopped = True
        if stopped and record is not None:
            self._discard_ownership(record)

    async def _ensure_server(self) -> asyncio.subprocess.Process:
        if self._server_process is not None and self._server_process.returncode is None:
            if self._server_ownership is not None and await self._managed_server_ready(
                self._server_ownership
            ):
                return self._server_process
            await self._stop_managed_server()
        elif self._server_process is not None:
            await self._stop_managed_server()

        await self._reclaim_stale_server()
        if await self._port_ready():
            raise RuntimeError("Whisper server port is already occupied by another process")
        server_command = self._server_command()
        command = [
            _platform_executable("/usr/bin/nice"),
            "-n",
            str(int(self.config.get("nice", 15))),
            *server_command,
        ]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        self._server_process = process
        try:
            record = self._new_ownership(process.pid, server_command)
            self._write_ownership(record)
            self._server_ownership = record
            deadline = time.monotonic() + float(self.config.get("server_start_timeout_seconds", 15))
            while time.monotonic() < deadline:
                if process.returncode is not None:
                    raise RuntimeError(f"Whisper server exited with code {process.returncode}")
                if await self._managed_server_ready(record):
                    return process
                await asyncio.sleep(0.1)
            raise RuntimeError("Whisper server did not become ready")
        except BaseException:
            await self._stop_managed_server()
            raise

    async def _transcribe_server(self, temporary: Path, prompt: str, timeout_seconds: float) -> str:
        async with self._server_lock:
            process = await self._ensure_server()
            record = self._server_ownership
            if (
                record is None
                or process.pid != record.server.pid
                or not await self._managed_server_ready(record)
            ):
                raise RuntimeError("Whisper server listener ownership could not be verified")
            form = aiohttp.FormData()
            with temporary.open("rb") as audio:
                form.add_field("file", audio, filename="speech.wav", content_type="audio/wav")
                form.add_field("response_format", "json")
                form.add_field("temperature", "0.0")
                if "beam_size" in self.config:
                    form.add_field("beam_size", str(max(1, min(5, int(self.config["beam_size"])))))
                if prompt:
                    form.add_field("prompt", prompt)
                timeout = aiohttp.ClientTimeout(total=timeout_seconds)
                url = f"http://127.0.0.1:{int(self.config.get('server_port', 18082))}/inference"
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(url, data=form) as response:
                        body = await response.text()
                        if response.status >= 400:
                            raise RuntimeError(
                                f"Whisper server HTTP {response.status}: {body[:300]}"
                            )
                        payload = json.loads(body)
            return str(payload.get("text", "")).strip()

    async def close(self) -> None:
        async with self._server_lock:
            await self._stop_managed_server()

    async def transcribe_verification(self, pcm: bytes, sample_rate: int = 16000) -> Transcript:
        available, reason = self.verification_available
        if not available:
            raise RuntimeError(reason)
        return await self._transcribe(
            pcm,
            sample_rate,
            model_path=self.verification_model_path,
            prompt=str(self.config.get("verification_prompt", self.config.get("prompt", ""))),
            threads=int(self.config.get("verification_threads", self.config.get("threads", 2))),
            nice=int(self.config.get("verification_nice", self.config.get("nice", 15))),
            timeout_seconds=float(
                self.config.get(
                    "verification_timeout_seconds", self.config.get("timeout_seconds", 45)
                )
            ),
            engine="whisper.cpp-verifier",
        )

    async def _transcribe(
        self,
        pcm: bytes,
        sample_rate: int,
        *,
        model_path: Path,
        prompt: str,
        threads: int,
        nice: int,
        timeout_seconds: float,
        engine: str,
    ) -> Transcript:
        if not pcm or len(pcm) % 2:
            raise ValueError("STT requires non-empty aligned s16le PCM")

        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="stt-", suffix=".wav", dir=self.runtime_dir
        )
        os.fchmod(descriptor, 0o600)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as raw_handle:
                with wave.open(raw_handle, "wb") as writer:
                    writer.setnchannels(1)
                    writer.setsampwidth(2)
                    writer.setframerate(sample_rate)
                    writer.writeframes(pcm)

            if (
                model_path == self.model_path
                and bool(self.config.get("persistent_server", True))
                and self.server_binary.is_file()
            ):
                started = time.monotonic()
                try:
                    raw = await self._transcribe_server(temporary, prompt, timeout_seconds)
                    return Transcript(
                        raw=raw,
                        engine=engine,
                        model=model_path.name,
                        latency_ms=(time.monotonic() - started) * 1000,
                    )
                except (
                    aiohttp.ClientError,
                    asyncio.TimeoutError,
                    json.JSONDecodeError,
                    OSError,
                    RuntimeError,
                    ValueError,
                ):
                    await self.close()
                except asyncio.CancelledError:
                    await self.close()
                    raise

            command = [
                _platform_executable("/usr/bin/nice"),
                "-n",
                str(nice),
                str(self.binary),
                "-m",
                str(model_path),
                "-f",
                str(temporary),
                "-t",
                str(max(1, min(4, threads))),
                "-l",
                str(self.config.get("language", "en")),
                "-nt",
                "-np",
                "-sns",
            ]
            prompt = prompt.strip()
            if "beam_size" in self.config:
                command.extend(["-bs", str(max(1, min(5, int(self.config["beam_size"]))))])
            if prompt:
                command.extend(["--prompt", prompt])
            started = time.monotonic()
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout_seconds,
                )
            except asyncio.CancelledError:
                # The CLI fallback is our own child. Reap it on a spoken
                # interruption instead of leaving CPU work running unseen.
                if process.returncode is None:
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
                raise
            except TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except TimeoutError:
                    process.kill()
                    await process.wait()
                raise RuntimeError("whisper.cpp transcription timed out") from None
            latency_ms = (time.monotonic() - started) * 1000
            if process.returncode != 0:
                detail = stderr.decode(errors="replace").strip().splitlines()
                raise RuntimeError(
                    f"whisper.cpp failed: {(detail[-1] if detail else 'unknown error')[:300]}"
                )
            raw = " ".join(
                line.strip()
                for line in stdout.decode(errors="replace").splitlines()
                if line.strip()
            )
            return Transcript(
                raw=raw.strip(), engine=engine, model=model_path.name, latency_ms=latency_ms
            )
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
