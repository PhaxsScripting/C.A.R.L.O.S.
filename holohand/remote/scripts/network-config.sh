#!/bin/sh
set -eu
# Phax rule: your firewall is yours. Never paste my laptop's rules over it.
printf '%s\n' 'Configure private Tailscale Serve HTTPS for 127.0.0.1:8765.' 'Keep the backend on loopback; do not use Funnel or expose its port publicly.' 'Set your own HTTPS origin in ~/.local/state/holohand-remote/config.json.' 'Review your firewall separately. This helper changes neither firewall nor DNS.'
