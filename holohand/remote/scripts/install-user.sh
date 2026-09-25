#!/bin/sh
set -eu
remote_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python3 -m venv "$remote_root/.venv"
"$remote_root/.venv/bin/pip" install -r "$remote_root/requirements.lock"
cd "$remote_root"
npm ci --ignore-scripts
npm run build
mkdir -p "$HOME/.local/bin" "$HOME/.config/autostart"
python3 - "$remote_root" <<'PY'
from pathlib import Path
import sys
root=Path(sys.argv[1]);home=Path.home();launcher=home/'.local/bin/holohand-remote'
launcher.write_text('#!/bin/sh\nexec "'+str(root/'.venv/bin/python')+'" "'+str(root/'scripts/control.py')+'" "$@"\n');launcher.chmod(0o700)
(home/'.config/autostart/holohand-remote.desktop').write_text('[Desktop Entry]\nType=Application\nName=HoloHand Remote\nExec='+str(launcher)+' start\nTerminal=false\nNoDisplay=true\nOnlyShowIn=KDE;\n')
PY
"$HOME/.local/bin/holohand-remote" restart
