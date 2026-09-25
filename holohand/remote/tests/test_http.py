"""Exercise the actual HTTP/WS middleware in an isolated state directory."""

import os, tempfile, sys, time
from pathlib import Path
import pytest
from aiohttp.test_utils import TestClient, TestServer
from aiohttp import ClientResponseError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["HOLOHAND_STATE"] = tempfile.mkdtemp(prefix="holohand-security-test-")
import app
from security import digest


@pytest.mark.asyncio
async def test_every_powerful_route_requires_authentication():
    async with TestClient(TestServer(app.create_app())) as client:
        origin = str(client.make_url("")).rstrip("/")
        app.security.origin = origin
        for route in [
            "system",
            "processes",
            "files",
            "files/read?path=.bashrc",
            "files/download?path=.bashrc",
            "terminals",
            "codex",
            "codex/events",
            "git",
            "ev",
            "devices",
            "events",
            "diagnostics",
            "monitors",
        ]:
            r = await client.get("/api/" + route)
            assert r.status == 401, route
        for route in [
            "terminals",
            "codex",
            "files/save",
            "files/action",
            "processes/signal",
            "action",
            "chat",
            "ev",
            "devices/revoke",
        ]:
            r = await client.post("/api/" + route, json={}, headers={"Origin": origin})
            assert r.status == 401, route
        for route in ["/ws/desktop", "/ws/terminal/invalid"]:
            with pytest.raises(ClientResponseError) as e:
                await client.ws_connect(route, origin=origin)
            assert e.value.status == 401
        r = await client.get("/api/system", cookies={"hh_session": "invalid"})
        assert r.status == 401


@pytest.mark.asyncio
async def test_csrf_host_origin_session_expiry_and_revoked_socket():
    async with TestClient(TestServer(app.create_app())) as client:
        origin = str(client.make_url("")).rstrip("/")
        app.security.origin = origin
        owner = "http-test"
        token = "isolated-test-token"
        app.security.devices[owner] = {"name": "test"}
        app.security.sessions[digest(token)] = {
            "device": owner,
            "expires": time.time() + 60,
            "authenticated": time.time(),
            "csrf": "csrf",
        }
        client.session.cookie_jar.update_cookies({"hh_session": token})
        r = await client.get("/api/system")
        assert r.status == 200
        app.security.sessions[digest(token)]["authenticated"] = time.time() - 301
        r = await client.post(
            "/api/desktop/authorize", json={}, headers={"Origin": origin, "X-HoloHand-CSRF": "csrf"}
        )
        assert r.status == 401 and "passkey again" in (await r.json())["error"]
        r = await client.get("/api/system")
        assert r.status == 200
        app.security.sessions[digest(token)]["authenticated"] = time.time()
        r = await client.post(
            "/api/desktop/authorize", json={}, headers={"Origin": origin, "X-HoloHand-CSRF": "csrf"}
        )
        assert r.status == 200
        r = await client.post("/api/terminals", json={}, headers={"Origin": origin})
        assert r.status == 403
        r = await client.post(
            "/api/terminals",
            json={},
            headers={"Origin": "https://evil.invalid", "X-HoloHand-CSRF": "csrf"},
        )
        assert r.status == 403
        r = await client.get("/api/system", headers={"Host": "evil.invalid"})
        assert r.status == 403
        r = await client.post(
            "/api/terminals",
            json={"cwd": "."},
            headers={"Origin": origin, "X-HoloHand-CSRF": "csrf"},
        )
        assert r.status == 200
        ident = (await r.json())["id"]
        ws = await client.ws_connect("/ws/terminal/" + ident, origin=origin)
        await app.security.revoke(owner)
        # Drain any final shell output, then prove transport closure.
        for _ in range(20):
            m = await ws.receive(timeout=3)
            if m.type.name in ("CLOSE", "CLOSED", "ERROR"):
                break
        assert ws.closed
        r = await client.get("/api/system")
        assert r.status == 401
        app.security.devices[owner] = {"name": "test"}
        app.security.sessions[digest(token)] = {
            "device": owner,
            "expires": 0,
            "authenticated": 0,
            "csrf": "csrf",
        }
        r = await client.get("/api/system")
        assert r.status == 401


@pytest.mark.asyncio
async def test_api_version_is_additive_and_unsupported_clients_are_rejected():
    async with TestClient(TestServer(app.create_app())) as client:
        app.security.origin = str(client.make_url("")).rstrip("/")
        for headers in ({}, {"X-Carlos-API-Version": "1"}):
            response = await client.get("/api/health", headers=headers)
            assert response.status == 200
            assert response.headers["X-Carlos-API-Version"] == "1"
            assert (await response.json())["compatible_api_versions"] == [1]
        response = await client.get("/api/health", headers={"X-Carlos-API-Version": "99"})
        assert response.status == 426
