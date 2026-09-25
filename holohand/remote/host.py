"""Bounded host facilities. No model output is interpreted as shell commands."""

import asyncio
import base64
import collections
import fcntl
import json
import os
from pathlib import Path
import pty
import secrets
import signal
import struct
import termios
import time
import uuid
import psutil

HOME = Path.home().resolve()


async def command(*args, timeout=15, cwd=None):
    p = await asyncio.create_subprocess_exec(
        *map(str, args),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        limit=2**20,
    )
    try:
        out, err = await asyncio.wait_for(p.communicate(), timeout)
    except (TimeoutError, asyncio.CancelledError):
        p.kill()
        await p.wait()
        raise
    return {
        "code": p.returncode,
        "stdout": out.decode(errors="replace")[:262144],
        "stderr": err.decode(errors="replace")[:8192],
    }


def homepath(value="."):
    p = Path(str(value)).expanduser()
    p = (HOME / p).resolve()
    if not p.is_relative_to(HOME):
        raise ValueError("Path must be inside your home folder.")
    return p


class HomeFiles:
    """Walk every parent using O_NOFOLLOW dirfds to prevent symlink escape races."""

    def __init__(self, root=HOME):
        self.root = root

    def parent(self, path):
        p = Path(str(path))
        if p.is_absolute():
            p = p.relative_to(self.root)
        parts = p.parts
        if not parts or any(x in ("..", "") for x in parts):
            raise ValueError("Invalid file path.")
        fd = os.open(self.root, os.O_DIRECTORY | os.O_RDONLY)
        try:
            for name in parts[:-1]:
                nxt = os.open(name, os.O_DIRECTORY | os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = nxt
            return fd, parts[-1]
        except BaseException:
            os.close(fd)
            raise

    def read(self, path, limit=2 * 1024 * 1024):
        parent, name = self.parent(path)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as stream:
                import stat

                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("Only regular files are supported.")
                data = stream.read(limit + 1)
                if len(data) > limit:
                    raise ValueError("File exceeds size limit.")
                return data
        finally:
            os.close(parent)

    def write(self, path, data, expected=None):
        import hashlib, stat

        parent, name = self.parent(path)
        temp = ".holohand-" + secrets.token_hex(12)
        try:
            mode = 0o600
            existed = False
            try:
                st = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISREG(st.st_mode):
                    raise ValueError("Only regular files can be edited.")
                mode = stat.S_IMODE(st.st_mode)
                existed = True
                if expected is None:
                    raise ValueError("Existing file needs a revision; reload it first.")
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
                with os.fdopen(fd, "rb") as s:
                    current = hashlib.sha256(s.read(2 * 1024 * 1024 + 1)).hexdigest()
                if current != expected:
                    raise ValueError("File changed on the computer. Reload before saving.")
            except FileNotFoundError:
                if expected:
                    raise ValueError("File was removed on the computer.")
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode, dir_fd=parent)
            with os.fdopen(fd, "wb") as s:
                s.write(data)
                s.flush()
                os.fsync(s.fileno())
            if existed:
                os.rename(temp, name, src_dir_fd=parent, dst_dir_fd=parent)
            else:
                os.link(temp, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                os.unlink(temp, dir_fd=parent)
        finally:
            try:
                os.unlink(temp, dir_fd=parent)
            except FileNotFoundError:
                pass
            os.close(parent)

    def listing(self, path="."):
        import stat

        if path in (".", "", str(self.root)):
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        else:
            parent, name = self.parent(path)
            try:
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            finally:
                os.close(parent)
        try:
            result = []
            for name in sorted(os.listdir(fd))[:3000]:
                try:
                    s = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    result.append(
                        {
                            "name": name,
                            "directory": stat.S_ISDIR(s.st_mode),
                            "symlink": stat.S_ISLNK(s.st_mode),
                            "size": s.st_size,
                            "modified": s.st_mtime,
                            "mode": oct(stat.S_IMODE(s.st_mode)),
                        }
                    )
                except OSError:
                    pass
            return result
        finally:
            os.close(fd)

    def mutate(self, operation, path, target=None):
        parent, name = self.parent(path)
        try:
            if operation == "mkdir":
                os.mkdir(name, 0o700, dir_fd=parent)
            elif operation == "delete":
                import stat

                s = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if stat.S_ISDIR(s.st_mode):
                    os.rmdir(name, dir_fd=parent)
                else:
                    os.unlink(name, dir_fd=parent)
            elif operation == "move":
                dest, dname = self.parent(target)
                try:
                    # renameat2 NOREPLACE prevents overwriting a destination raced into place.
                    import ctypes

                    libc = ctypes.CDLL(None, use_errno=True)
                    if libc.renameat2(parent, os.fsencode(name), dest, os.fsencode(dname), 1):
                        errno = ctypes.get_errno()
                        raise OSError(errno, os.strerror(errno))
                finally:
                    os.close(dest)
            elif operation == "copy":
                self.write(target, self.read(path, 32 * 1024 * 1024))
            else:
                raise ValueError("Unknown file action.")
        finally:
            os.close(parent)


def system_snapshot():
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage(HOME)
    battery = psutil.sensors_battery()
    try:
        temps = [
            {"label": x.label or name, "celsius": x.current}
            for name, values in psutil.sensors_temperatures().items()
            for x in values
        ]
    except (OSError, AttributeError):
        temps = []
    return {
        "host": os.uname().nodename,
        "cpu": psutil.cpu_percent(),
        "ram": {"used": vm.used, "total": vm.total},
        "swap": psutil.swap_memory()._asdict(),
        "load": os.getloadavg(),
        "disk": disk._asdict(),
        "network": psutil.net_io_counters()._asdict(),
        "uptime": time.time() - psutil.boot_time(),
        "temperatures": temps,
        "battery": battery._asdict() if battery else None,
        "time": time.time(),
    }


class Terminal:
    def __init__(self, owner, cwd):
        self.id = secrets.token_hex(16)
        self.owner = owner
        self.cwd = cwd
        self.history = collections.deque()
        self.bytes = 0
        self.seq = 0
        self.clients = set()
        self.proc = None
        self.last_seen = time.time()

    async def start(self):
        master, slave = pty.openpty()
        self.master = master
        os.set_blocking(master, False)

        def setup():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor")
        self.proc = await asyncio.create_subprocess_exec(
            "/bin/bash",
            "-i",
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=self.cwd,
            env=env,
            preexec_fn=setup,
        )
        os.close(slave)
        asyncio.get_running_loop().add_reader(master, self.read)
        self.resize(90, 26)

    def read(self):
        try:
            data = os.read(self.master, 16384)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            asyncio.get_running_loop().remove_reader(self.master)
            return
        self.seq += 1
        msg = {"seq": self.seq, "data": base64.b64encode(data).decode()}
        self.history.append(msg)
        self.bytes += len(data)
        while self.bytes > 131072 and self.history:
            self.bytes -= len(base64.b64decode(self.history.popleft()["data"]))
        for q in list(self.clients):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                self.clients.discard(q)

    def resize(self, cols, rows):
        fcntl.ioctl(
            self.master,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", max(2, min(200, rows)), max(10, min(400, cols)), 0, 0),
        )

    def write(self, data):
        if len(data) > 65536:
            raise ValueError("Terminal input too large.")
        os.write(self.master, data)

    async def close(self):
        if self.proc and self.proc.returncode is None:
            try:
                os.killpg(self.proc.pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except TimeoutError:
                os.killpg(self.proc.pid, signal.SIGKILL)
                await self.proc.wait()
        asyncio.get_running_loop().remove_reader(self.master)
        os.close(self.master)


async def ev_request(kind, payload=None):
    path = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "ev/ev.sock"
    reader, writer = await asyncio.open_unix_connection(path, limit=1048576)
    try:
        await asyncio.wait_for(reader.readline(), 5)
        request_id = uuid.uuid4().hex
        writer.write(
            (json.dumps({"type": kind, "id": request_id, "payload": payload or {}}) + "\n").encode()
        )
        await writer.drain()
        async with asyncio.timeout(130):
            while raw := await reader.readline():
                message = json.loads(raw)
                if message.get("id") == request_id or message.get("type") == "error":
                    return message
        raise RuntimeError("Carlos disconnected.")
    finally:
        writer.close()
        await writer.wait_closed()
