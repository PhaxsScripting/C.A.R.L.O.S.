#!/bin/sh
set -eu

data_home=${XDG_DATA_HOME:-"$HOME/.local/share"}
state_home=${XDG_STATE_HOME:-"$HOME/.local/state"}
pointer="$state_home/ev/last-install-backup"
bin_home="$HOME/.local/bin"

if [ ! -r "$pointer" ]; then
    printf '%s\n' "No E.V. install backup pointer was found." >&2
    exit 1
fi
backup_root=$(sed -n '1p' "$pointer")
manifest="$backup_root/manifest.tsv"
if [ ! -r "$manifest" ]; then
    printf '%s\n' "E.V. rollback manifest is missing: $manifest" >&2
    exit 1
fi

if [ -x "$bin_home/evctl" ]; then
    "$bin_home/evctl" stop >/dev/null 2>&1 || true
    sleep 0.2
fi

retained="$backup_root/removed-current"
mkdir -p "$retained"
chmod 700 "$retained"
tab=$(printf '\t')
while IFS="$tab" read -r target previous; do
    key=$(basename -- "$target")
    if [ -e "$target" ] || [ -L "$target" ]; then
        suffix=0
        destination="$retained/$key"
        while [ -e "$destination" ]; do
            suffix=$((suffix + 1))
            destination="$retained/$key.$suffix"
        done
        mv -- "$target" "$destination"
    fi
    if [ "$previous" != - ] && { [ -e "$previous" ] || [ -L "$previous" ]; }; then
        mkdir -p "$(dirname -- "$target")"
        mv -- "$previous" "$target"
    fi
done < "$manifest"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$data_home/applications" >/dev/null 2>&1 || true
fi

printf '%s\n' "E.V. application files and startup entry were rolled back."
printf '%s\n' "Runtime data and explicit memories were preserved under $data_home/ev."
printf '%s\n' "Removed current files are recoverable at $retained."
