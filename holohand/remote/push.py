"""Opt-in Web Push with restricted provider destinations and no private content."""

import asyncio, json, time
from pathlib import Path
from urllib.parse import urlparse
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from pywebpush import webpush, WebPushException
from security import save, b64


class Push:
    def __init__(self, state):
        self.state = state
        self.key = state / "push-key.pem"
        self.path = state / "push-subscriptions.json"
        if not self.key.exists():
            key = ec.generate_private_key(ec.SECP256R1())
            self.key.write_bytes(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            self.key.chmod(0o600)
        key = serialization.load_pem_private_key(self.key.read_bytes(), password=None)
        self.public = b64(
            key.public_key().public_bytes(
                serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
            )
        )
        self.subs = json.loads(self.path.read_text()) if self.path.exists() else {}

    def subscribe(self, owner, sub):
        url = urlparse(sub.get("endpoint", ""))
        host = url.hostname or ""
        if (
            url.scheme != "https"
            or url.port not in (None, 443)
            or url.username
            or url.password
            or url.fragment
            or not (
                host == "web.push.apple.com"
                or host.endswith(".push.apple.com")
                or host == "fcm.googleapis.com"
                or host.endswith(".push.services.mozilla.com")
            )
        ):
            raise ValueError("Unsupported push provider.")
        if not isinstance(sub.get("keys"), dict) or not all(
            isinstance(sub["keys"].get(k), str) and len(sub["keys"][k]) < 300
            for k in ("auth", "p256dh")
        ):
            raise ValueError("Invalid push keys.")
        self.subs[owner] = sub
        save(self.path, self.subs)

    def revoke(self, owner):
        self.subs.pop(owner, None)
        save(self.path, self.subs)

    async def send(self, kind):
        messages = {
            "codex.finished": "Codex finished a task.",
            "codex.waiting": "Codex needs your input.",
            "ev.offline": "E.V. is offline.",
            "ev.online": "E.V. is available.",
            "agent.started": "HoloHand restarted.",
            "system.temperature": "Your computer is running hot.",
            "system.disk": "Your computer is running low on disk space.",
            "device.paired": "A new device paired with HoloHand.",
            "device.login": "A device signed in to HoloHand.",
        }
        if kind not in messages:
            return
        for owner, sub in list(self.subs.items()):
            try:
                await asyncio.to_thread(
                    webpush,
                    subscription_info=sub,
                    data=json.dumps({"title": "HoloHand", "body": messages[kind]}),
                    vapid_private_key=str(self.key),
                    vapid_claims={"sub": "https://tailscale.com"},
                    timeout=10,
                    ttl=300,
                )
            except WebPushException as e:
                if e.response is not None and e.response.status_code in (404, 410):
                    self.revoke(owner)
            except Exception:
                pass
