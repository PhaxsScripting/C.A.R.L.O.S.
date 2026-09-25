#!/bin/sh
set -eu
[ "$(uname -s)" = Linux ] && [ "$(id -u)" = 0 ] || exit 1
input_user=${1:?Usage: setup-linux-input.sh DESKTOP_USER}
case "$input_user" in *[!a-zA-Z0-9_-]*|'') echo 'Invalid username' >&2; exit 1;; esac
[ "$(id -u "$input_user")" != 0 ] || exit 1
input_rule="KERNEL==\"uinput\", SUBSYSTEM==\"misc\", OWNER=\"$input_user\", MODE=\"0600\""
# Explicit, single-user permission. No world-writable input or root vision process.
modprobe uinput
module_file=/etc/modules-load.d/holohand.conf
mkdir -p /etc/modules-load.d
if [ -e "$module_file" ];then
 grep -qx uinput "$module_file" || { echo 'Existing module file preserved';exit 1; }
else
 printf 'uinput\n' > "$module_file"
 chmod 644 "$module_file"
fi
rule=/etc/udev/rules.d/71-holohand-uinput.rules
if [ -e "$rule" ]; then
 grep -Fqx "$input_rule" "$rule" || { echo 'Existing different rule preserved';exit 1; }
else
 printf '%s\n' "$input_rule" > "$rule"
 chmod 644 "$rule"
fi
udevadm control --reload-rules
udevadm trigger --action=change --sysname-match=uinput
udevadm settle
ls -l /dev/uinput
