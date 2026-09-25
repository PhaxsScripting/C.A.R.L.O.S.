# Security

This project can control a desktop, execute commands, and access files. Pair only
your own devices and review every permission. Keep the remote backend on loopback
and use private HTTPS access; do not expose it with a public port forward or
Tailscale Funnel. Changing the HTTPS origin can require pairing again.

Keep credentials, passkeys, recovery material, recordings, screenshots, and local
state out of Git. The repository includes no production keys. Tests use disposable
fixtures. Do not post tokens or private logs in public issues.

For a vulnerability, use GitHub's private vulnerability reporting if enabled on
this repository. Otherwise open a minimal issue asking for a private contact
without including exploit details or secrets.
