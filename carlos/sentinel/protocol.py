"""Versioned, signed wake envelopes. Keys never cross the network."""

import hashlib, hmac, json, re, secrets, socket, sqlite3, time, os


def canonical(message):
    return json.dumps(message, sort_keys=True, separators=(",", ":")).encode()


def sign(device, action, key, *, now=None):
    message = {
        "version": 1,
        "device": device,
        "action": action,
        "timestamp": int(time.time() if now is None else now),
        "nonce": secrets.token_hex(16),
    }
    return {**message, "signature": hmac.new(key, canonical(message), hashlib.sha256).hexdigest()}


class Verifier:
    def __init__(self, path, devices):
        self.devices = devices
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(path)
        self.db.executescript(
            "CREATE TABLE IF NOT EXISTS nonces(device TEXT,nonce TEXT,timestamp INTEGER,PRIMARY KEY(device,nonce)); CREATE TABLE IF NOT EXISTS wake_rate(device TEXT PRIMARY KEY,timestamp INTEGER);"
        )

    def verify(self, envelope, *, now=None):
        now = int(time.time() if now is None else now)
        if not isinstance(envelope, dict) or set(envelope) != {
            "version",
            "device",
            "action",
            "timestamp",
            "nonce",
            "signature",
        }:
            raise ValueError("Invalid envelope")
        e = envelope.copy()
        signature = e.pop("signature")
        device = self.devices.get(e["device"]) if isinstance(e["device"], str) else None
        if not device or device.get("revoked"):
            raise ValueError("Unknown or revoked device")
        if e["version"] != 1 or type(e["timestamp"]) is not int or abs(now - e["timestamp"]) > 30:
            raise ValueError("Expired or unsupported envelope")
        if e["action"] not in {"status", "wake"} or e["action"] not in device["capabilities"]:
            raise ValueError("Capability denied")
        if not isinstance(e["nonce"], str) or not re.fullmatch("[a-f0-9]{32}", e["nonce"]):
            raise ValueError("Invalid nonce")
        key = bytes.fromhex(device["key"])
        if (
            len(key) != 32
            or not isinstance(signature, str)
            or not hmac.compare_digest(
                signature, hmac.new(key, canonical(e), hashlib.sha256).hexdigest()
            )
        ):
            raise ValueError("Invalid signature")
        with self.db:
            try:
                self.db.execute("INSERT INTO nonces VALUES(?,?,?)", (e["device"], e["nonce"], now))
            except sqlite3.IntegrityError as error:
                raise ValueError("Replay rejected") from error
            if e["action"] == "wake":
                previous = self.db.execute(
                    "SELECT timestamp FROM wake_rate WHERE device=?", (e["device"],)
                ).fetchone()
                if previous and now - previous[0] < 10:
                    raise ValueError("Wake rate limit")
                self.db.execute("INSERT OR REPLACE INTO wake_rate VALUES(?,?)", (e["device"], now))
            self.db.execute("DELETE FROM nonces WHERE timestamp < ?", (now - 86400,))
        return e["action"]


def magic_packet(mac):
    if not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", mac):
        raise ValueError("Invalid fixed target MAC")
    return b"\xff" * 6 + bytes.fromhex(mac.replace(":", "")) * 16


def send_wake(mac, broadcast):
    # Target comes only from the owner-controlled node configuration, never the request.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(magic_packet(mac), (broadcast, 9))
