#!/usr/bin/env python3
"""Preview or restore one user install; never touch conversations or Plasma state."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time


def allowed_targets(home, data, config):
    return {
        data / "ev/app",
        *(
            home / ".local/bin" / name
            for name in ("ev-core", "evctl", "carlosctl", "ev-ui", "ev-activate", "ev-panel-state")
        ),
        data / "applications/ev-control-center.desktop",
        config / "autostart/ev-core.desktop",
        config / "autostart/ev-shell.desktop",
        data / "icons/hicolor/scalable/apps/ev-control-center.svg",
        data / "dbus-1/services/com.ev.Core.service",
    }


def read_plan(backup, allowed):
    backup = backup.resolve(strict=True)
    plan = []
    for line in (backup / "manifest.tsv").read_text().splitlines():
        target_text, previous_text = line.split("\t")
        target = Path(target_text)
        if target not in allowed or any(target == item[0] for item in plan):
            raise ValueError("Manifest has an unexpected or duplicate target")
        previous = None if previous_text == "-" else Path(previous_text)
        if previous is not None:
            if not previous.exists() or not previous.resolve().is_relative_to(backup / "files"):
                raise ValueError("Backup entry is missing or escapes its backup directory")
        plan.append((target, previous))
    if not plan:
        raise ValueError("Empty rollback manifest")
    return plan


def restore(plan, backup):
    # All copies must finish before any installed file is moved.
    with tempfile.TemporaryDirectory(prefix="restore-stage-", dir=backup) as staging:
        staged = []
        for index, (target, previous) in enumerate(plan):
            source = Path(staging) / str(index)
            if previous is not None:
                if previous.is_dir():
                    shutil.copytree(previous, source, symlinks=True)
                else:
                    shutil.copy2(previous, source)
            staged.append((target, source if previous is not None else None))
        retained = Path(tempfile.mkdtemp(prefix="replaced-", dir=backup))
        applied = []
        try:
            for index, (target, source) in enumerate(staged):
                old = retained / str(index)
                existed = target.exists() or target.is_symlink()
                if existed:
                    target.rename(old)
                applied.append((target, old if existed else None))
                if source is not None:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source.rename(target)
        except BaseException:
            for index, (target, old) in reversed(list(enumerate(applied))):
                if target.exists() or target.is_symlink():
                    target.rename(retained / ("failed-" + str(index)))
                if old is not None:
                    old.rename(target)
            raise
        (retained / "manifest.tsv").write_text(
            "".join(f'{target}\t{old or "-"}\n' for target, old in applied)
        )
        return retained


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    home = Path.home()
    data = Path(os.environ.get("XDG_DATA_HOME", home / ".local/share"))
    config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    state = Path(os.environ.get("XDG_STATE_HOME", home / ".local/state"))
    backup = args.backup.resolve(strict=True)
    if not backup.is_relative_to((state / "ev/install-backups").resolve()):
        raise ValueError("Select a Carlos user-install backup")
    plan = read_plan(backup, allowed_targets(home, data, config))
    for target, previous in plan:
        print(f'{target} <- {previous or "absent before installation"}')
    if not args.apply:
        print("Preview only; use --apply to stop Carlos, restore and restart.")
        return
    ctl = home / ".local/bin/evctl"
    # A partially completed install may not yet have restored its launcher.
    # Use the reviewed source CLI when the installed launcher cannot run.
    try:
        stopped = subprocess.run(
            [str(ctl), "stop"],
            timeout=8,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        fallback = stopped.returncode != 0
    except (OSError, subprocess.TimeoutExpired):
        fallback = True
    if fallback:
        source = Path(__file__).resolve().parents[1] / "core"
        try:
            subprocess.run(
                ["/usr/bin/python3", "-m", "ev.cli", "stop"],
                env={**os.environ, "PYTHONPATH": str(source)},
                timeout=8,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except subprocess.TimeoutExpired:
            pass
    for _ in range(150):
        result = subprocess.run(
            [
                "gdbus",
                "call",
                "--session",
                "--dest",
                "org.freedesktop.DBus",
                "--object-path",
                "/org/freedesktop/DBus",
                "--method",
                "org.freedesktop.DBus.NameHasOwner",
                "com.ev.Core",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
        if "false" in result.stdout:
            break
        time.sleep(0.2)
    else:
        raise RuntimeError("Core did not stop; installed files were not restored")
    retained = restore(plan, backup)
    print("Replaced files retained at", retained)
    activate = home / ".local/bin/ev-activate"
    if activate.exists():
        subprocess.run([str(activate)], timeout=45, check=True)
        subprocess.run([str(ctl), "health"], timeout=5, check=True)


if __name__ == "__main__":
    main()
