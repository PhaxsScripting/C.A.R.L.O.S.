from __future__ import annotations
import ctypes
import os
from pathlib import Path
import shutil
import socket
import struct
import sys

IS_FREEBSD = sys.platform.startswith("freebsd")


def executable(original: str) -> str:
    """Preserve audited Linux paths; resolve FreeBSD base/ports binaries centrally."""
    if not IS_FREEBSD:
        return original
    name = Path(original).name
    if name == "python3":
        return sys.executable
    candidates = [
        Path(prefix) / name
        for prefix in (
            "/usr/local/bin",
            "/usr/local/sbin",
            "/usr/bin",
            "/usr/sbin",
            "/bin",
            "/sbin",
        )
    ]
    if name == "qdbus6":
        candidates += [Path("/usr/local/lib/qt6/bin/qdbus"), Path("/usr/local/bin/qdbus")]
    return next((str(p) for p in candidates if p.is_file() and os.access(p, os.X_OK)), original)


def peer_uid(sock) -> int | None:
    """Fail closed; never substitute filesystem mode for peer credentials."""
    if sock is None:
        return None
    try:
        if IS_FREEBSD:
            uid = ctypes.c_uint()
            gid = ctypes.c_uint()
            lib = ctypes.CDLL(None, use_errno=True)
            lib.getpeereid.argtypes = [
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_uint),
                ctypes.POINTER(ctypes.c_uint),
            ]
            lib.getpeereid.restype = ctypes.c_int
            if lib.getpeereid(sock.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
                return None
            return uid.value
        if hasattr(socket, "SO_PEERCRED"):
            return struct.unpack(
                "3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            )[1]
    except (OSError, ValueError, AttributeError, struct.error):
        pass
    return None


def sysctl_text(name: str) -> str | None:
    import subprocess

    try:
        r = subprocess.run(
            ["/sbin/sysctl", "-n", name], capture_output=True, text=True, timeout=2, check=False
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def temperature() -> dict:
    value = sysctl_text("dev.cpu.0.temperature")
    try:
        return {"celsius": round(float(value.rstrip("C")), 1), "sensor": "dev.cpu.0.temperature"}
    except (ValueError, AttributeError):
        return {"celsius": None, "sensor": None}


def boot_id() -> str:
    import psutil

    return "freebsd:" + str(int(psutil.boot_time() * 1_000_000))


def process_identity(pid: int) -> dict | None:
    import psutil

    try:
        p = psutil.Process(pid)
        start = p.create_time()
        uid = p.uids().real
        exe = p.exe()
        cmd = p.cmdline()
        if not cmd or psutil.Process(pid).create_time() != start:
            return None
        return {
            "pid": pid,
            "uid": uid,
            "exe": os.path.realpath(exe),
            "cmdline": cmd,
            "start_time": str(int(start * 1_000_000)),
        }
    except (psutil.Error, OSError, ValueError):
        return None


def firewall_report() -> dict:
    import subprocess

    observations = []
    for binary, args in [("/sbin/pfctl", ["-s", "info"]), ("/sbin/ipfw", ["-a", "list"])]:
        if not Path(binary).is_file():
            continue
        try:
            r = subprocess.run([binary, *args], capture_output=True, text=True, timeout=5)
            observations.append(
                {
                    "name": Path(binary).name,
                    "readable": r.returncode == 0,
                    "stdout": r.stdout[:20000],
                    "stderr": r.stderr[:2000],
                }
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            observations.append(
                {"name": Path(binary).name, "readable": False, "error": type(e).__name__}
            )
    readable = any(o["readable"] for o in observations)
    active = [
        o["name"]
        for o in observations
        if o["name"] == "pfctl" and o["readable"] and "Status: Enabled" in o.get("stdout", "")
    ]
    if sysctl_text("net.inet.ip.fw.enable") == "1":
        active.append("ipfw")
    runtime = {
        "backend": "freebsd-pf-ipfw",
        "readable": readable,
        "verified": False,
        "read_method": "native_unprivileged",
        "rules_changed": False,
        "services": observations,
        "admin_authorization_required": not readable,
        "rules_readable": any(o["name"] == "ipfw" and o["readable"] for o in observations),
        "message": "Native status observations only; policy effectiveness is not inferred. Permission denied means unknown.",
    }
    return {
        **runtime,
        "status": "ACTIVE" if active else "INACTIVE_OR_UNKNOWN",
        "active_implementations": active,
        "rules_readable_as_user": runtime["rules_readable"],
        "non_comment_rule_lines": None,
        "confidence": "HIGH" if active else "LOW",
        "runtime": runtime,
        "evidence": ["native pfctl -s info / ipfw -a list; read-only, no elevation"],
        "limitations": ["No automatic privilege escalation or firewall configuration changes."],
    }
