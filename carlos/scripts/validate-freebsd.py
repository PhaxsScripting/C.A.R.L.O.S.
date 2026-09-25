#!/usr/bin/env python3
import argparse
import importlib
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

parser = argparse.ArgumentParser()
parser.add_argument("--preinstall", action="store_true")
args = parser.parse_args()
if not sys.platform.startswith("freebsd") or os.getuid() == 0:
    raise SystemExit("Validate natively as a normal user on FreeBSD")
from ev.platform.system import peer_uid, process_identity, temperature, executable
from ev.platform.config_defaults import defaults
from ev.config import DEFAULT_CONFIG

for name in (
    "aiohttp",
    "psutil",
    "jeepney",
    "dbus",
    "gi",
    "numpy",
    "onnxruntime",
    "sherpa_onnx",
    "rapidocr",
    "ev.service",
):
    importlib.import_module(name)
    print("PASS import", name)
a, b = socket.socketpair()
try:
    assert peer_uid(a) == os.getuid()
finally:
    a.close()
    b.close()
assert process_identity(os.getpid())["uid"] == os.getuid()
assert defaults(DEFAULT_CONFIG)["voice"]["stt"]["binary"] == "/usr/local/bin/whisper-cli"
for binary in (
    "piper",
    "whisper-cli",
    "whisper-server",
    "pactl",
    "parec",
    "paplay",
    "qdbus6",
    "gdbus",
):
    path = Path(executable("/usr/bin/" + binary))
    assert path.is_file(), binary
with tempfile.TemporaryDirectory(prefix="ev-validation-") as folder:
    db = sqlite3.connect(Path(folder) / "memory-test.sqlite")
    db.execute("create table test(value text)")
    db.execute("insert into test values (?)", ("isolated test",))
    db.commit()
    assert db.execute("pragma integrity_check").fetchone()[0] == "ok"
    db.close()
print("PASS native credentials, process identity, SQLite, executable paths, configuration")
if args.preinstall:
    print("PENDING graphical session, audio, model loading, provider and desktop actions")
    raise SystemExit(0)
if os.environ.get("XDG_SESSION_TYPE") != "x11" or not os.environ.get("DISPLAY"):
    raise SystemExit("Run session checks from Konsole inside Plasma X11")
bindir = Path.home() / ".local/bin"
for command in ([str(bindir / "evctl"), "health"], ["/usr/local/bin/pactl", "info"]):
    subprocess.run(command, check=True, timeout=15, stdout=subprocess.DEVNULL)
from ev.platform.x11_input import X11Input

control = X11Input()
assert control.connect()["connected"]
control.close()
print("PASS E.V. IPC, Pulse server connection, X11 connection (no input injected)")
print(
    "MANUAL: open E.V.; send text; push-to-talk; test microphone/TTS/wake; confirm transcript and response."
)
print(
    "MANUAL: two disposable windows: inspect geometry, move/resize/match, swap monitors, maximize/restore, close."
)
print(
    "MANUAL: security status; notification; pause/cancel; logout/login autostart; clean shutdown."
)
print("Provider: run evctl setup-nvidia or setup-openai locally; never put credentials on p2.")
