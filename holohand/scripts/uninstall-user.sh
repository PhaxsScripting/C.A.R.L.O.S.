#!/bin/sh
set -eu
[ "$(id -u)" != 0 ] || exit 1
"$HOME/.local/bin/holohand" --quit || :
rm -f "$HOME/.config/autostart/holohand.desktop" "$HOME/.local/share/applications/holohand.desktop" "$HOME/.local/bin/holohand" "$HOME/.local/bin/holohand-benchmark"
rm -rf "$HOME/.local/share/holohand"
rm -rf "$HOME/.local/lib/holohand"
echo 'HoloHand removed. Calibration/logs/backups kept. E.V. and Plasma untouched.'
echo 'Optional Linux permission rollback: remove only /etc/udev/rules.d/71-holohand-uinput.rules, reload udev, restore /dev/uinput root:root 0600.'
