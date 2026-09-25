#!/usr/bin/env python3
"""Fetch the pinned hand models. No mystery weights in the Git history."""

import hashlib
import json
from pathlib import Path
import tempfile
import urllib.request


def main():
    model_dir = Path(__file__).resolve().parents[1] / "models"
    manifest = json.loads((model_dir / "sources.json").read_text())
    for item in manifest["models"]:
        target = model_dir / item["filename"]
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == item["sha256"]:
            print(f"Already verified: {target.name}")
            continue
        with tempfile.NamedTemporaryFile(dir=model_dir, suffix=".download") as pending:
            with urllib.request.urlopen(item["url"], timeout=60) as response:
                while chunk := response.read(1024 * 1024):
                    pending.write(chunk)
            pending.flush()
            data = Path(pending.name).read_bytes()
            if hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise RuntimeError(f"Hash mismatch: {target.name}")
            target.write_bytes(data)
        print(f"Verified: {target.name}")


if __name__ == "__main__":
    main()
