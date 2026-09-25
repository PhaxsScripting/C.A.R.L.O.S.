"""Fetch the existing public Qwen model natively, keeping p2 and Gentoo space free."""

import hashlib
import os
from pathlib import Path
import shutil
import sys
import urllib.request

if not sys.platform.startswith("freebsd") or os.getuid() == 0:
    raise SystemExit("Run as a normal user on FreeBSD")
base = Path(sys.argv[1]) / "models/qwen2.5-1.5b-instruct"
base.mkdir(parents=True, exist_ok=True)
target = base / "qwen2.5-1.5b-instruct-q4_k_m.gguf"
expected = "6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e"


def digest(p):
    with p.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


if target.exists():
    if digest(target) != expected:
        raise SystemExit("Existing model hash mismatch; preserved for investigation")
else:
    if shutil.disk_usage(base).free < 8 * 1024**3:
        raise SystemExit("Need 8 GiB free before model download/install copies")
    url = "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/91cad51170dc346986eccefdc2dd33a9da36ead9/qwen2.5-1.5b-instruct-q4_k_m.gguf"
    temporary = target.with_suffix(".downloading")
    with urllib.request.urlopen(url, timeout=60) as source, temporary.open("wb") as dest:
        total = 0
        while chunk := source.read(1024 * 1024):
            total += len(chunk)
            if total > 1117320736:
                raise SystemExit("Unexpected model size")
            dest.write(chunk)
    if temporary.stat().st_size != 1117320736 or digest(temporary) != expected:
        raise SystemExit("Downloaded model failed verification")
    temporary.rename(target)
print("Verified public Qwen model; no credentials used")
