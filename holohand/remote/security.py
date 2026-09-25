"""Local pairing, verified passkeys, short sessions, and immediate revocation."""

import base64
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlparse
from aiohttp import web
from webauthn import (
    generate_registration_options,
    generate_authentication_options,
    verify_registration_response,
    verify_authentication_response,
    options_to_json,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    ResidentKeyRequirement,
    UserVerificationRequirement,
    PublicKeyCredentialDescriptor,
)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def unb64(data):
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def save(path, data):
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


class Security:
    def __init__(self, state, origin):
        self.state = state
        self.origin = origin
        self.rp = urlparse(origin).hostname
        self.secure = origin.startswith("https://")
        self.path = state / "devices.json"
        self.devices = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.sessions = {}
        self.challenges = {}
        self.failures = []
        self.on_revoke = None
        self.emit = None

    def persist(self):
        save(self.path, self.devices)

    def session(self, req, fresh=False, optional=False):
        token = req.cookies.get("hh_session", "")
        s = self.sessions.get(digest(token))
        if not s or s["expires"] < time.time() or s["device"] not in self.devices:
            if optional:
                return None
            raise web.HTTPUnauthorized(text="Unlock HoloHand with your passkey.")
        if fresh and s["authenticated"] < time.time() - 300:
            raise web.HTTPUnauthorized(text="Use your passkey again for this action.")
        return s

    def issue(self, device):
        token = secrets.token_urlsafe(32)
        now = time.time()
        self.sessions[digest(token)] = {
            "device": device,
            "expires": now + 900,
            "authenticated": now,
            "csrf": secrets.token_urlsafe(24),
        }
        self.devices[device]["last_seen"] = now
        self.persist()
        r = web.json_response(
            {"ok": True, "csrf": self.sessions[digest(token)]["csrf"], "expires": now + 900}
        )
        r.set_cookie(
            "hh_session",
            token,
            httponly=True,
            secure=self.secure,
            samesite="Strict",
            max_age=900,
            path="/",
        )
        return r

    def limit(self):
        now = time.time()
        self.failures = [t for t in self.failures if t > now - 300]
        if len(self.failures) >= 10:
            raise web.HTTPTooManyRequests(text="Too many attempts. Wait five minutes.")
        self.failures.append(now)

    def challenge(self, kind, data):
        now = time.time()
        self.challenges = {k: v for k, v in self.challenges.items() if v["expires"] > now}
        key = secrets.token_urlsafe(32)
        self.challenges[digest(key)] = dict(data, kind=kind, expires=now + 120)
        return key

    def take(self, req, kind):
        c = self.challenges.pop(digest(req.cookies.get("hh_challenge", "")), None)
        if not c or c["kind"] != kind or c["expires"] < time.time():
            raise web.HTTPForbidden(text="Authentication challenge expired. Try again.")
        return c

    def options_response(self, options, key):
        r = web.json_response(json.loads(options_to_json(options)))
        r.set_cookie(
            "hh_challenge",
            key,
            httponly=True,
            secure=self.secure,
            samesite="Strict",
            max_age=120,
            path="/",
        )
        return r

    async def pair_options(self, req):
        self.limit()
        data = await req.json()
        p = self.state / "pairing.json"
        pairing = json.loads(p.read_text()) if p.exists() else {}
        if pairing.get("expires", 0) < time.time() or not secrets.compare_digest(
            pairing.get("hash", ""), digest(str(data.get("code", "")))
        ):
            raise web.HTTPForbidden(text="Invalid or expired pairing code.")
        # Consume the one-time code before offering registration.
        p.unlink()
        device = secrets.token_hex(16)
        name = str(data.get("name", "iPhone"))[:80]
        opts = generate_registration_options(
            rp_id=self.rp,
            rp_name="HoloHand",
            user_id=device.encode(),
            user_name=name,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
        )
        key = self.challenge(
            "register", {"challenge": opts.challenge, "device": device, "name": name}
        )
        return self.options_response(opts, key)

    async def pair_finish(self, req):
        c = self.take(req, "register")
        data = await req.json()
        verified = verify_registration_response(
            credential=data,
            expected_challenge=c["challenge"],
            expected_rp_id=self.rp,
            expected_origin=self.origin,
            require_user_verification=True,
        )
        self.devices[c["device"]] = {
            "name": c["name"],
            "credential": b64(verified.credential_id),
            "key": b64(verified.credential_public_key),
            "counter": verified.sign_count,
            "created": time.time(),
        }
        if self.emit:
            await self.emit("device.paired")
        return self.issue(c["device"])

    async def login_options(self, req):
        self.limit()
        opts = generate_authentication_options(
            rp_id=self.rp, user_verification=UserVerificationRequirement.REQUIRED
        )
        return self.options_response(opts, self.challenge("login", {"challenge": opts.challenge}))

    async def login_finish(self, req):
        c = self.take(req, "login")
        data = await req.json()
        found = next(
            ((k, d) for k, d in self.devices.items() if d["credential"] == data.get("id")), None
        )
        if not found:
            raise web.HTTPForbidden(text="Passkey is not paired or was revoked.")
        key, device = found
        verified = verify_authentication_response(
            credential=data,
            expected_challenge=c["challenge"],
            expected_rp_id=self.rp,
            expected_origin=self.origin,
            credential_public_key=unb64(device["key"]),
            credential_current_sign_count=device["counter"],
            require_user_verification=True,
        )
        device["counter"] = verified.new_sign_count
        if self.emit:
            await self.emit("device.login")
        return self.issue(key)

    async def revoke(self, device):
        self.devices.pop(device, None)
        self.persist()
        self.sessions = {k: v for k, v in self.sessions.items() if v["device"] != device}
        if self.on_revoke:
            await self.on_revoke(device)
