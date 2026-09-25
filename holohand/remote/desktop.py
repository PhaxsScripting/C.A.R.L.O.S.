"""KDE's RDP server + Apache Guacamole transport. Loopback only; pinned RDP TLS."""

import asyncio
import json
import os
from pathlib import Path
import re
import secrets
import time
from host import command


def instruction(*parts):
    return ",".join(f"{len(str(x))}.{x}" for x in parts) + ";"


async def read_instruction(reader):
    args = []
    while True:
        size = await reader.readuntil(b".")
        if len(size) > 9 or not size[:-1].isdigit():
            raise ValueError("Invalid Guacamole instruction.")
        length = int(size[:-1])
        if length > 16 * 1024 * 1024:
            raise ValueError("Guacamole instruction is too large.")
        args.append((await reader.readexactly(length)).decode())
        delimiter = await reader.readexactly(1)
        if delimiter == b";":
            return args
        if delimiter != b",":
            raise ValueError("Invalid Guacamole delimiter.")


class Desktop:
    def __init__(self, state, log):
        self.state = state
        self.log = log
        self.krdp = None
        self.guacd = None
        self.lock = asyncio.Lock()
        self.active = None

    async def monitors(self):
        r = await command("kscreen-doctor", "-j")
        data = json.loads(r["stdout"])
        result = []
        for out in data.get("outputs", []):
            if out.get("connected") and out.get("enabled"):
                mode = next(
                    (m for m in out.get("modes", []) if m["id"] == out.get("currentModeId")), None
                )
                result.append(
                    {
                        "name": out["name"],
                        "id": len(result),
                        "mode": mode,
                        "position": out.get("pos"),
                    }
                )
        return result

    async def stop(self):
        for p in (self.krdp, self.guacd):
            if p and p.returncode is None:
                p.terminate()
                try:
                    await asyncio.wait_for(p.wait(), 5)
                except TimeoutError:
                    p.kill()
                    await p.wait()
        self.krdp = self.guacd = None
        self.active = None

    async def connect(self, monitor, quality):
        if self.lock.locked():
            raise ValueError("Desktop is already in use. Disconnect it first.")
        await self.lock.acquire()
        try:
            profiles = {
                "saver": (1280, 720, 25, 35),
                "balanced": (1920, 1080, 30, 65),
                "ultra": (2560, 1440, 60, 90),
                "auto": (1920, 1080, 30, 65),
            }
            width, height, fps, q = profiles[quality]
            monitors = await self.monitors()
            if monitor < -1 or monitor >= len(monitors):
                raise ValueError("Display is unavailable.")
            # A fresh random gateway-only password, never the desktop login password.
            password = secrets.token_urlsafe(32)
            env = dict(os.environ, HOLOHAND_RDP_PASSWORD=password, HOLOHAND_MAX_FPS=str(fps))
            exe = Path.home() / ".local/opt/holohand-krdp/bin/krdpserver"
            if not exe.exists():
                raise RuntimeError("HoloHand private KDE RDP build is not installed.")
            cert = self.state / "rdp.crt"
            key = self.state / "rdp.key"
            if not cert.exists():
                raise RuntimeError("RDP certificate is not configured.")
            args = [
                str(exe),
                "--address",
                "127.0.0.1",
                "--port",
                "13389",
                "--username",
                "holohand",
                "--certificate",
                str(cert),
                "--certificate-key",
                str(key),
                "--monitor",
                str(monitor),
                "--quality",
                str(q),
            ]
            self.krdp = await asyncio.create_subprocess_exec(
                *args, env=env, stdout=self.log, stderr=self.log
            )
            guacd = Path.home() / ".local/opt/holohand-guacamole/sbin/guacd"
            self.guacd = await asyncio.create_subprocess_exec(
                str(guacd),
                "-f",
                "-b",
                "127.0.0.1",
                "-l",
                "14822",
                "-L",
                "warning",
                stdout=self.log,
                stderr=self.log,
                env=dict(os.environ, LD_LIBRARY_PATH=str(guacd.parent.parent / "lib")),
            )
            for _ in range(60):
                if self.krdp.returncode is not None or self.guacd.returncode is not None:
                    raise RuntimeError("Desktop service exited. Check diagnostics.")
                try:
                    if not any(
                        line.split()[1] == "0100007F:344D" and line.split()[3] == "0A"
                        for line in Path("/proc/net/tcp").read_text().splitlines()[1:]
                    ):
                        raise OSError("RDP starting")
                    reader, writer = await asyncio.open_connection("127.0.0.1", 14822)
                    break
                except OSError:
                    await asyncio.sleep(0.1)
            else:
                raise RuntimeError("Desktop services did not become ready.")
            writer.write(instruction("select", "rdp").encode())
            await writer.drain()
            args = await asyncio.wait_for(read_instruction(reader), 10)
            if args[0] != "args":
                raise RuntimeError("Invalid gateway handshake.")
            fingerprint = (
                (
                    await command(
                        "openssl", "x509", "-in", cert, "-noout", "-fingerprint", "-sha256"
                    )
                )["stdout"]
                .strip()
                .split("=")[-1]
            )
            settings = {
                "hostname": "127.0.0.1",
                "port": "13389",
                "username": "holohand",
                "password": password,
                "security": "tls",
                "ignore-cert": "false",
                "cert-fingerprints": "sha256:" + fingerprint,
                "disable-audio": "true",
                "enable-wallpaper": "true",
                "enable-theming": "true",
                "enable-font-smoothing": "true",
                "enable-full-window-drag": "true",
                "color-depth": "32",
                "resize-method": "display-update",
                "enable-drive": "false",
                "enable-printing": "false",
                "disable-upload": "true",
                "disable-download": "true",
                "server-layout": "en-us-qwerty",
                "max-fps": str(fps),
                "force-lossless": "false",
            }
            for inst in [
                instruction("size", width, height, 96),
                instruction("audio"),
                instruction("video"),
                instruction("image", "image/png", "image/jpeg", "image/webp"),
                instruction("timezone", "America/New_York"),
                instruction(
                    "connect",
                    *[k if k.startswith("VERSION_") else settings.get(k, "") for k in args[1:]],
                ),
            ]:
                writer.write(inst.encode())
            await writer.drain()
            self.active = {
                "monitor": monitor,
                "quality": quality,
                "target_fps": fps,
                "started": time.time(),
                "bytes": 0,
            }
            return reader, writer
        except BaseException:
            await self.stop()
            self.lock.release()
            raise

    async def release(self):
        await self.stop()
        if self.lock.locked():
            self.lock.release()
