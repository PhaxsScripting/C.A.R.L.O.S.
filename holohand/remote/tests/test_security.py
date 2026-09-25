import asyncio
import json
import os
from pathlib import Path
import sys
import time
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from security import Security, digest, save
from host import HomeFiles
from aiohttp import web


class Request:
    def __init__(self, cookies=None, data=None):
        self.cookies = cookies or {}
        self.data = data

    async def json(self):
        return self.data


def test_expired_invalid_and_revoked_sessions(tmp_path):
    s = Security(tmp_path, "https://host.example.ts.net")
    s.devices["phone"] = {"name": "Phone"}
    s.sessions[digest("valid")] = {
        "device": "phone",
        "expires": time.time() + 30,
        "authenticated": time.time(),
    }
    assert s.session(Request({"hh_session": "valid"}))["device"] == "phone"
    with pytest.raises(web.HTTPUnauthorized):
        s.session(Request({"hh_session": "invalid"}))
    s.sessions[digest("valid")]["expires"] = 0
    with pytest.raises(web.HTTPUnauthorized):
        s.session(Request({"hh_session": "valid"}))
    s.sessions[digest("valid")]["expires"] = time.time() + 30
    asyncio.run(s.revoke("phone"))
    with pytest.raises(web.HTTPUnauthorized):
        s.session(Request({"hh_session": "valid"}))


def test_pairing_expiry_rate_limit_and_one_use(tmp_path):
    s = Security(tmp_path, "https://host.example.ts.net")
    save(tmp_path / "pairing.json", {"hash": digest("123456"), "expires": 0})
    with pytest.raises(web.HTTPForbidden):
        asyncio.run(s.pair_options(Request(data={"code": "123456"})))
    save(tmp_path / "pairing.json", {"hash": digest("123456"), "expires": time.time() + 60})
    r = asyncio.run(s.pair_options(Request(data={"code": "123456"})))
    assert r.status == 200 and not (tmp_path / "pairing.json").exists()
    with pytest.raises(web.HTTPForbidden):
        asyncio.run(s.pair_options(Request(data={"code": "123456"})))
    for i in range(7):
        with pytest.raises(web.HTTPForbidden):
            asyncio.run(s.pair_options(Request(data={"code": "bad"})))
    with pytest.raises(web.HTTPTooManyRequests):
        asyncio.run(s.pair_options(Request(data={"code": "bad"})))


def test_files_no_escape_symlinks_or_overwrite(tmp_path):
    root = tmp_path / "home"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("private")
    f = HomeFiles(root)
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        f.read("link/secret")
    with pytest.raises(ValueError):
        f.read("../outside/secret")
    with pytest.raises(OSError):
        f.write("link/secret", b"overwrite")
    f.write("file", b"one")
    with pytest.raises(ValueError):
        f.write("file", b"two")
    f.write("file", b"two", digest("one"))
    with pytest.raises(ValueError):
        f.write("file", b"three", digest("one"))
    f.write("other", b"original")
    with pytest.raises(OSError):
        f.mutate("move", "file", "other")
    assert f.read("other") == b"original" and f.read("file") == b"two"


def test_fifo_is_not_read(tmp_path):
    os.mkfifo(tmp_path / "fifo")
    with pytest.raises(ValueError):
        HomeFiles(tmp_path).read("fifo")
