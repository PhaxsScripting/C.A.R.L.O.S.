#!/usr/bin/env python3
"""Read-only, bounded hardware/software audit; no serials, keys or conversations."""

from pathlib import Path
import subprocess, json, time, os, psutil, shutil

ROOT = Path(__file__).resolve().parents[1]


def command(*args):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=8)
        return {"code": p.returncode, "output": p.stdout[:20000], "error": p.stderr[:500]}
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"unavailable": type(e).__name__}


def read(p):
    try:
        return Path(p).read_text().strip()
    except OSError:
        return None


r = {
    "observed_at": time.time(),
    "hardware": {
        k: read("/sys/class/dmi/id/" + k)
        for k in ("sys_vendor", "product_name", "board_name", "bios_version", "bios_date")
    },
    "memory": psutil.virtual_memory()._asdict(),
    "disk": psutil.disk_usage(str(Path.home()))._asdict(),
    "power": {k: read("/sys/power/" + k) for k in ("state", "mem_sleep")},
    "network": command("ip", "-br", "link"),
    "ethernet": command("ethtool", "eno2"),
    "audio": command("wpctl", "status"),
    "pci": command("lspci", "-nn"),
    "usb": command("lsusb"),
    "ports": command("ss", "-lnt"),
    "services": command("rc-status", "default"),
    "gpu": command("vainfo", "--display", "drm", "--device", "/dev/dri/renderD128"),
    "dns": read("/etc/resolv.conf"),
    "versions": {},
}
for name, args in {
    "kernel": ["uname", "-r"],
    "gcc": ["gcc", "-dumpfullversion"],
    "clang": ["clang", "--version"],
    "cmake": ["cmake", "--version"],
    "ninja": ["ninja", "--version"],
    "python": ["python3", "--version"],
    "node": ["node", "--version"],
    "npm": ["npm", "--version"],
    "rust": ["rustc", "--version"],
    "cargo": ["cargo", "--version"],
    "java": ["java", "-version"],
    "kotlin": ["kotlinc", "-version"],
    "adb": ["adb", "version"],
    "git": ["git", "--version"],
    "ffmpeg": ["ffmpeg", "-version"],
    "gstreamer": ["gst-launch-1.0", "--version"],
    "pipewire": ["pipewire", "--version"],
    "wireplumber": ["wireplumber", "--version"],
    "qt": ["qmake6", "-query", "QT_VERSION"],
    "kwin": ["kwin_wayland", "--version"],
}.items():
    if name == "kwin":
        continue  # Never launch a second compositor even as a version probe.
    r["versions"][name] = command(*args)
procs = []
for p in psutil.process_iter(["name", "cmdline", "memory_info"]):
    try:
        args = " ".join(p.info["cmdline"] or [])
        if (
            p.info["name"] in ("ev-ui", "holohand", "whisper-server")
            or "-m ev" in args
            or "remote/app.py" in args
        ):
            p.cpu_percent()
            procs.append(p)
    except psutil.Error:
        pass
time.sleep(5)
r["process_sample"] = []
for p in procs:
    try:
        r["process_sample"].append(
            {
                "name": p.name(),
                "pid": p.pid,
                "rss_mib": round(p.memory_info().rss / 1048576, 2),
                "cpu_percent_one_core": p.cpu_percent(),
            }
        )
    except psutil.Error:
        pass
r["sample_seconds"] = 5
path = Path.home() / ".local/state/carlos/audit.json"
path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
path.write_text(json.dumps(r, indent=2))
path.chmod(0o600)
print(path)
