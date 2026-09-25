#!/usr/bin/env python3
"""Reference node for separate always-on hardware. Loopback HTTP only."""

import argparse, json, os, stat
from pathlib import Path
from aiohttp import web
from protocol import Verifier, send_wake


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True, type=Path)
    p.add_argument("--port", type=int, default=8788)
    a = p.parse_args()
    if stat.S_IMODE(a.config.stat().st_mode) & 0o077:
        raise SystemExit("Configuration must be owner-only (0600)")
    config = json.loads(a.config.read_text())
    verify = Verifier(a.config.with_suffix(".nonces.db"), config["devices"])

    async def request(req):
        # Read revocations on every request; nonce state survives process restarts.
        verify.devices = json.loads(a.config.read_text())["devices"]
        try:
            action = verify.verify(await req.json())
        except (ValueError, TypeError, KeyError):
            return web.json_response({"error": "Request rejected"}, status=403)
        if action == "wake":
            send_wake(config["target_mac"], config["broadcast"])
        return web.json_response(
            {
                "version": 1,
                "action": action,
                "packet_sent": action == "wake",
                "workstation_awake": "UNVERIFIED",
                "sentinel": "ONLINE",
            }
        )

    app = web.Application(client_max_size=2048)
    app.router.add_post("/v1/request", request)
    web.run_app(app, host="127.0.0.1", port=a.port, access_log=None, print=None)


if __name__ == "__main__":
    main()
