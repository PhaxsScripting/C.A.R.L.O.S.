#!/usr/bin/env python3
"""Fail on common private artifacts in the Git index; never print secret values."""

from pathlib import Path
import re
import subprocess
import sys

patterns = {
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub token": re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    "provider token": re.compile(rb"(?:sk-proj-|nvapi-)[A-Za-z0-9_-]{25,}"),
    "personal path": re.compile(rb"/home/phax(?:/|\b)"),
    "private tailnet": re.compile(rb"https://[a-z0-9-]+\.tail[a-z0-9]+\.ts\.net", re.I),
}
blocked_parts = {".venv", "node_modules", "__pycache__", "build", "dist", "test-results"}
blocked_suffixes = {".db", ".sqlite", ".pem", ".key", ".gguf", ".onnx", ".pyc", ".log"}
failures = []
for raw in subprocess.check_output(["git", "ls-files", "-z"]).split(b"\0"):
    if not raw:
        continue
    name = raw.decode()
    path = Path(name)
    if blocked_parts.intersection(path.parts) or path.suffix in blocked_suffixes:
        failures.append((name, "private/generated artifact"))
    data = subprocess.check_output(["git", "show", ":" + name])
    if name == "tools/check-release.py":
        continue  # The rules contain literal patterns, not credentials.
    for rule, pattern in patterns.items():
        if pattern.search(data):
            failures.append((name, rule))
for name, rule in failures:
    print(f"{name}: {rule}")
print(f"Release scan: {len(failures)} findings (heuristic, not a security guarantee)")
sys.exit(bool(failures))
