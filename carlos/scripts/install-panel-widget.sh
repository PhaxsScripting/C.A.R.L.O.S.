#!/bin/sh
set -eu
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
kpackagetool6 --type Plasma/Applet --install "$project_root/plasma/org.phax.ev.voiceactivity"
printf '%s\n' 'Installed. Add Carlos through Plasma Edit Mode → Add Widgets.'
