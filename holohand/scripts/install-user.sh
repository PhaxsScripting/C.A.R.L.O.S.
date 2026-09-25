#!/bin/sh
set -eu
[ "$(id -u)" != 0 ] || { echo 'Install as normal desktop user';exit 1; }
root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
state="$HOME/.local/state/holohand"
mkdir -p "$state" "$HOME/.local/bin" "$HOME/.local/share/applications" "$HOME/.config/autostart"
chmod 700 "$state"
backup=$(mktemp -d "$state/install-$(date +%Y%m%d-%H%M%S).XXXXXX")
for name in bin/holohand bin/holohand-benchmark lib/holohand share/holohand share/applications/holohand.desktop;do
 if [ -e "$HOME/.local/$name" ];then mkdir -p "$backup/$(dirname "$name")";cp -Rp "$HOME/.local/$name" "$backup/$name";fi
done
[ ! -f "$HOME/.config/autostart/holohand.desktop" ] || cp "$HOME/.config/autostart/holohand.desktop" "$backup/autostart.desktop"
cmake --install "$root/build" --prefix "$HOME/.local"
cat > "$HOME/.local/share/applications/holohand.desktop" <<ENTRY
[Desktop Entry]
Type=Application
Name=HoloHand
Comment=Local hand-tracking desktop control
Exec=$HOME/.local/bin/holohand --calibrate
Icon=input-mouse
Categories=Utility;Accessibility;
Terminal=false
ENTRY
cat > "$HOME/.config/autostart/holohand.desktop" <<ENTRY
[Desktop Entry]
Type=Application
Name=HoloHand tracking
Exec=$HOME/.local/bin/holohand --background
Icon=input-mouse
Terminal=false
ENTRY
printf '%s\n' "$backup" > "$state/last-install"
echo 'Installed independent HoloHand. First-run calibration gates input.'
