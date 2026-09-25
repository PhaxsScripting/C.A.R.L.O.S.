#!/bin/sh
set -eu
# Run through the desktop's pkexec prompt. No password is read by this script.
record=/var/lib/holohand-remote-install
install -d -m 700 "$record"
if [ ! -f "$record/world.before" ]; then
 cp -a /var/lib/portage/world "$record/world.before"
 nft list ruleset > "$record/nftables.before"
 rc-update show > "$record/services.before"
fi
usefile=/etc/portage/package.use/holohand-remote
if [ -e "$usefile" ] && [ ! -e "$record/package.use.before" ]; then
 cp -a "$usefile" "$record/package.use.before"
fi
printf '%s\n' 'net-misc/freerdp server -sdl' > "$usefile"
emerge --usepkg --noreplace --jobs=1 --load-average=4 net-vpn/tailscale '=kde-plasma/krdp-6.6.6'
rc-update add tailscale default
rc-service tailscale start
# Set a Tailscale operator separately for your own desktop account.
nft list ruleset > "$record/nftables.after"
printf '%s\n' 'System dependencies installed. Tailscale login remains account-owned.'
