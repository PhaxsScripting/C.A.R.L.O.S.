import json
import os
from pathlib import Path
import shutil
import sys

if not sys.platform.startswith("freebsd") or os.getuid() == 0:
    raise SystemExit("Run as a normal user on FreeBSD")
state = Path.home() / ".local/state/ev"
backup = Path((state / "last-freebsd-backup").read_text().strip())
if backup.parent != state / "freebsd-backups":
    raise SystemExit("Unexpected backup")
for row in reversed(json.loads((backup / "manifest.json").read_text())):
    target = Path(row["path"])
    if not target.is_relative_to(Path.home()):
        raise SystemExit("Invalid target")
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)
    if row["backup"]:
        (
            shutil.copytree(row["backup"], target, symlinks=True)
            if Path(row["backup"]).is_dir() and not Path(row["backup"]).is_symlink()
            else shutil.copy2(row["backup"], target, follow_symlinks=False)
        )
print(
    "Restored only E.V. assets; backups and private data retained. Log out/in for activation state."
)
