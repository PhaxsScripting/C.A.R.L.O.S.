#!/usr/bin/env python3
"""Install only E.V. user assets, retaining an exact independent rollback."""

import datetime
import json
import os
import pathlib
import shutil
import sys

if not sys.platform.startswith("freebsd") or os.getuid() == 0:
    raise SystemExit("Run as a normal user on FreeBSD")
root = pathlib.Path(__file__).resolve().parents[1]
home = pathlib.Path.home()
state = home / ".local/state/ev"
stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
backup = state / "freebsd-backups" / stamp
backup.mkdir(parents=True, mode=0o700)
manifest = []


def replace(target, source=None, text=None):
    target = pathlib.Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    old = backup / str(len(manifest))
    exists = target.exists() or target.is_symlink()
    manifest.append({"path": str(target), "backup": str(old) if exists else None})
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2))
    if exists:
        shutil.move(str(target), old)
    if source:
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
    else:
        target.write_text(text)


# Build/runtime remains in its versioned source folder; venv shebangs stay valid.
runtime = home / ".local/share/ev/runtime/freebsd"
replace(runtime, text="")
runtime.unlink()
runtime.symlink_to(pathlib.Path(sys.argv[1]).resolve(), target_is_directory=True)
app = home / ".local/share/ev/app"
staged = root / "build/freebsd/app"
staged.mkdir(exist_ok=True)
for name in ("core", "assets"):
    if (staged / name).exists():
        shutil.rmtree(staged / name)
    shutil.copytree(
        root / name, staged / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
(staged / "bin").mkdir(exist_ok=True)
shutil.copy2(root / "build/ui/ev-ui", staged / "bin/ev-ui")
replace(app, source=staged)
bindir = home / ".local/bin"
for name, component in [("ev-core", "core"), ("evctl", "cli")]:
    replace(
        bindir / name,
        text=f'#!/bin/sh\nexport PYTHONPATH="{app}/core"\nexec "{runtime}/bin/python" -m ev.platform.launch {component} "$@"\n',
    )
    (bindir / name).chmod(0o755)
for name in ("ev-ui", "ev-activate", "ev-panel-state"):
    text = (
        (root / "scripts" / name)
        .read_text()
        .replace("/usr/bin/gdbus", "/usr/local/bin/gdbus")
        .replace("/usr/bin/python3", str(runtime / "bin/python"))
    )
    replace(bindir / name, text=text)
    (bindir / name).chmod(0o755)
for template, target in [
    ("ev-control-center.desktop.in", home / ".local/share/applications/ev-control-center.desktop"),
    ("ev-core.desktop.in", home / ".config/autostart/ev-core.desktop"),
    ("ev-shell.desktop.in", home / ".config/autostart/ev-shell.desktop"),
    ("com.ev.Core.service.in", home / ".local/share/dbus-1/services/com.ev.Core.service"),
]:
    text = (root / "packaging" / template).read_text()
    for key, value in [("EV_UI", "ev-ui"), ("EV_CORE", "ev-core"), ("EV_ACTIVATE", "ev-activate")]:
        text = text.replace("@" + key + "@", str(bindir / value))
    replace(target, text=text)
replace(
    home / ".local/share/icons/hicolor/scalable/apps/ev-control-center.svg",
    source=root / "packaging/ev-control-center.svg",
)
# Pretrained public model files only; never private config, provider.env or DBs.
models = pathlib.Path(sys.argv[2]) / "models"
if models.exists():
    for source in sorted(models.rglob("*")):
        if source.is_file():
            target = home / ".local/share/ev/models" / source.relative_to(models)
            if not target.exists():
                replace(target, source=source)
(home / ".config/ev").mkdir(parents=True, exist_ok=True, mode=0o700)
(state / "last-freebsd-backup").write_text(str(backup))
print("Installed E.V.; private configuration and memory were not imported or overwritten.")
print("Rollback: python3.11 " + str(root / "scripts/rollback-freebsd.py"))
