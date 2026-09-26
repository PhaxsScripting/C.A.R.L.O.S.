#!/bin/sh
set -eu
pet_project=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
pet_data=${XDG_DATA_HOME:-"$HOME/.local/share"}
pet_state=${XDG_STATE_HOME:-"$HOME/.local/state"}
pet_binary="$pet_project/build/ui/ev-pet"
if [ ! -x "$pet_binary" ]; then
    printf '%s\n' 'Build carlos/ui first; ev-pet is missing.' >&2
    exit 1
fi
# Install just the buddy. No core restart or login startup changes.
python3 - "$pet_project" "$pet_data" "$pet_state" <<'PY'
import json, os, shutil, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path
project, data, state = map(Path, sys.argv[1:])
backup = state / "ev/pet-backups" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
backup.mkdir(parents=True, mode=0o700)
launcher = Path.home() / ".local/bin/carlos-pet"
entries = [
    (data / "ev/app/bin/ev-pet", (project / "build/ui/ev-pet").read_bytes(), 0o755),
    (launcher, (project / "scripts/carlos-pet").read_bytes(), 0o755),
    (data / "applications/carlos-pet.desktop", (project / "packaging/carlos-pet.desktop.in").read_text().replace("@CARLOS_PET@", '"' + str(launcher).replace('\\', '\\\\').replace('"', '\\"') + '"').encode(), 0o644),
]
manifest = []
for index, (target, _, _) in enumerate(entries):
    previous = backup / str(index)
    existed = target.exists()
    if existed:
        shutil.copy2(target, previous)
    manifest.append({"target": str(target), "previous": str(previous) if existed else None})
(backup / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
try:
    for target, content, mode in entries:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=".pet-install-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
            os.chmod(temporary, mode)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
except BaseException:
    for entry in manifest:
        if entry["previous"]:
            shutil.copy2(entry["previous"], entry["target"])
        else:
            Path(entry["target"]).unlink(missing_ok=True)
    raise
print("Carlos Pet installed. Open Carlos Pet from your app menu.")
print("Previous files and manifest:", backup)
PY
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$pet_data/applications" >/dev/null 2>&1 || true
fi
