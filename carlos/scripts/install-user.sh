#!/bin/sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
data_home=${XDG_DATA_HOME:-"$HOME/.local/share"}
config_home=${XDG_CONFIG_HOME:-"$HOME/.config"}
state_home=${XDG_STATE_HOME:-"$HOME/.local/state"}
bin_home="$HOME/.local/bin"
app_root="$data_home/ev/app"
applications_dir="$data_home/applications"
autostart_dir="$config_home/autostart"
icon_dir="$data_home/icons/hicolor/scalable/apps"
dbus_services_dir="$data_home/dbus-1/services"
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_root="$state_home/ev/install-backups/$timestamp"
manifest="$backup_root/manifest.tsv"
start_core=true

if [ "${1:-}" = "--no-start" ]; then
    start_core=false
fi

if [ ! -x "$project_root/build/ui/ev-ui" ]; then
    printf '%s\n' "Carlos UI has not been built: $project_root/build/ui/ev-ui" >&2
    exit 1
fi

PYTHONPATH="$project_root/core" /usr/bin/python3 -m unittest discover -s "$project_root/tests" >/dev/null

mkdir -p "$backup_root/files" "$bin_home" "$applications_dir" "$autostart_dir" "$icon_dir" "$dbus_services_dir" "$(dirname -- "$app_root")"
chmod 700 "$backup_root" "$(dirname -- "$app_root")"
: > "$manifest"
chmod 600 "$manifest"

backup_target() {
    key=$1
    target=$2
    if [ -e "$target" ] || [ -L "$target" ]; then
        previous="$backup_root/files/$key"
        mv -- "$target" "$previous"
    else
        previous=-
    fi
    printf '%s\t%s\n' "$target" "$previous" >> "$manifest"
}

checksum_or_missing() {
    if [ -f "$1" ]; then
        sha256sum "$1" | cut -d ' ' -f 1
    else
        printf '%s' MISSING
    fi
}

panel_file="$config_home/plasma-org.kde.plasma.desktop-appletsrc"
kdeglobals_file="$config_home/kdeglobals"
kwin_file="$config_home/kwinrc"
panel_before=$(checksum_or_missing "$panel_file")
kdeglobals_before=$(checksum_or_missing "$kdeglobals_file")
kwin_before=$(checksum_or_missing "$kwin_file")
printf 'panel\t%s\nkdeglobals\t%s\nkwin\t%s\n' "$panel_before" "$kdeglobals_before" "$kwin_before" > "$backup_root/plasma-checksums.before.tsv"

stage_root=$(mktemp -d "$(dirname -- "$app_root")/.ev-stage.XXXXXX")
installation_changed=false
rollback_attempted=false
finish_install() {
    result=$?
    trap - EXIT HUP INT TERM
    rm -rf -- "$stage_root"
    if [ "$result" -ne 0 ] && [ "$installation_changed" = true ] && [ "$rollback_attempted" = false ]; then
        printf '%s\n' "Installation interrupted; restoring the recorded previous files." >&2
        if ! /usr/bin/python3 "$project_root/scripts/rollback-user.py" "$backup_root" --apply; then
            printf '%s\n' "Restore needs attention. Preserved manifest: $manifest" >&2
        fi
    fi
    exit "$result"
}
trap finish_install EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir -p "$stage_root/app/core" "$stage_root/app/bin" "$stage_root/app/assets/voice" "$stage_root/app/assets/kwin"
cp -a -- "$project_root/core/." "$stage_root/app/core/"
cp -a -- "$project_root/assets/voice/." "$stage_root/app/assets/voice/"
cp -a -- "$project_root/assets/kwin/." "$stage_root/app/assets/kwin/"
install -m 755 "$project_root/build/ui/ev-ui" "$stage_root/app/bin/ev-ui"
if [ -x "$project_root/build/ui/ev-pet" ]; then
    install -m 755 "$project_root/build/ui/ev-pet" "$stage_root/app/bin/ev-pet"
fi

installation_changed=true
backup_target app "$app_root"
backup_target ev-core "$bin_home/ev-core"
backup_target evctl "$bin_home/evctl"
backup_target carlosctl "$bin_home/carlosctl"
backup_target ev-ui "$bin_home/ev-ui"
backup_target carlos-pet "$bin_home/carlos-pet"
backup_target pet-desktop "$applications_dir/carlos-pet.desktop"
backup_target ev-activate "$bin_home/ev-activate"
backup_target ev-panel-state "$bin_home/ev-panel-state"
backup_target desktop "$applications_dir/ev-control-center.desktop"
backup_target autostart "$autostart_dir/ev-core.desktop"
backup_target shell-autostart "$autostart_dir/ev-shell.desktop"
backup_target icon "$icon_dir/ev-control-center.svg"
backup_target dbus-service "$dbus_services_dir/com.ev.Core.service"

mv -- "$stage_root/app" "$app_root"

install -m 755 "$project_root/scripts/ev-core" "$bin_home/ev-core"
install -m 755 "$project_root/scripts/evctl" "$bin_home/evctl"
install -m 755 "$project_root/scripts/carlosctl" "$bin_home/carlosctl"
install -m 755 "$project_root/scripts/ev-ui" "$bin_home/ev-ui"
if [ -x "$app_root/bin/ev-pet" ]; then
    install -m 755 "$project_root/scripts/carlos-pet" "$bin_home/carlos-pet"
    sed "s|@CARLOS_PET@|$bin_home/carlos-pet|g" "$project_root/packaging/carlos-pet.desktop.in" > "$applications_dir/carlos-pet.desktop"
fi
install -m 755 "$project_root/scripts/ev-activate" "$bin_home/ev-activate"
install -m 755 "$project_root/scripts/ev-panel-state" "$bin_home/ev-panel-state"
install -m 644 "$project_root/packaging/ev-control-center.svg" "$icon_dir/ev-control-center.svg"

sed "s|@EV_UI@|$bin_home/ev-ui|g" "$project_root/packaging/ev-control-center.desktop.in" > "$stage_root/ev-control-center.desktop"
sed "s|@EV_ACTIVATE@|$bin_home/ev-activate|g" "$project_root/packaging/ev-core.desktop.in" > "$stage_root/ev-core.desktop"
sed "s|@EV_UI@|$bin_home/ev-ui|g" "$project_root/packaging/ev-shell.desktop.in" > "$stage_root/ev-shell.desktop"
sed "s|@EV_CORE@|$bin_home/ev-core|g" "$project_root/packaging/com.ev.Core.service.in" > "$stage_root/com.ev.Core.service"
install -m 644 "$stage_root/ev-control-center.desktop" "$applications_dir/ev-control-center.desktop"
install -m 644 "$stage_root/ev-core.desktop" "$autostart_dir/ev-core.desktop"
install -m 644 "$stage_root/ev-shell.desktop" "$autostart_dir/ev-shell.desktop"
install -m 644 "$stage_root/com.ev.Core.service" "$dbus_services_dir/com.ev.Core.service"

printf '%s\n' "$backup_root" > "$state_home/ev/last-install-backup"
chmod 600 "$state_home/ev/last-install-backup"

panel_after=$(checksum_or_missing "$panel_file")
kdeglobals_after=$(checksum_or_missing "$kdeglobals_file")
kwin_after=$(checksum_or_missing "$kwin_file")
printf 'panel\t%s\nkdeglobals\t%s\nkwin\t%s\n' "$panel_after" "$kdeglobals_after" "$kwin_after" > "$backup_root/plasma-checksums.after.tsv"
if [ "$panel_before" != "$panel_after" ] || [ "$kdeglobals_before" != "$kdeglobals_after" ] || [ "$kwin_before" != "$kwin_after" ]; then
    printf '%s\n' "Safety check failed: a protected Plasma file changed during install." >&2
    exit 1
fi

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$applications_dir" >/dev/null 2>&1 || true
fi

if [ "$start_core" = true ]; then
    if "$bin_home/evctl" health >/dev/null 2>&1; then
        "$bin_home/evctl" pause-wake >/dev/null 2>&1 || true
        "$bin_home/evctl" stop >/dev/null 2>&1 || true
        attempts=0
        while [ "$attempts" -lt 150 ]; do
            if ! /usr/bin/gdbus call --session \
                --dest org.freedesktop.DBus \
                --object-path /org/freedesktop/DBus \
                --method org.freedesktop.DBus.NameHasOwner com.ev.Core 2>/dev/null | grep -q true; then
                break
            fi
            attempts=$((attempts + 1))
            sleep 0.2
        done
    fi
    /usr/bin/gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus --method org.freedesktop.DBus.ReloadConfig >/dev/null 2>&1 || true
    "$bin_home/ev-activate" >/dev/null || true
    ready=false
    attempts=0
    while [ "$attempts" -lt 40 ]; do
        if "$bin_home/evctl" health >/dev/null 2>&1; then
            ready=true
            break
        fi
        attempts=$((attempts + 1))
        sleep 0.1
    done
    if [ "$ready" != true ]; then
        printf '%s\n' "New Carlos core did not become healthy; restoring the previous installation." >&2
        rollback_attempted=true
        if /usr/bin/python3 "$project_root/scripts/rollback-user.py" "$backup_root" --apply; then
            printf '%s\n' "Previous installation restored and health-checked; the failed version was retained in the rollback directory." >&2
        else
            printf '%s\n' "Automatic rollback did not finish. Preserved rollback manifest: $backup_root/manifest.tsv" >&2
        fi
        exit 1
    fi
fi

printf '%s\n' "Carlos installed for user $(id -un)."
printf '%s\n' "Backup and rollback manifest: $backup_root"
printf '%s\n' "Protected Plasma checksums are unchanged."
