#!/usr/bin/env python3
"""HoloHand Remote: authenticated, tailnet-only mobile gateway."""

import asyncio
import base64
import codecs
import collections
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import mimetypes
import os
from pathlib import Path
import secrets
import signal
import time
from urllib.parse import urlparse
from aiohttp import web, WSMsgType
import psutil
from security import Security, save, digest
from host import command, homepath, HomeFiles, system_snapshot, Terminal, ev_request
from codex_bridge import CodexBridge
from desktop import Desktop
from push import Push

ROOT = Path(__file__).resolve().parent
STATE = Path(os.environ.get("HOLOHAND_STATE", str(Path.home() / ".local/state/holohand-remote")))
STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
os.umask(0o077)
CONFIG = STATE / "config.json"
config = json.loads(CONFIG.read_text()) if CONFIG.exists() else {"origin": "http://localhost:8765"}
security = Security(STATE, config["origin"])
push = Push(STATE)
log = logging.getLogger("holohand")
log.setLevel(logging.INFO)
handler = RotatingFileHandler(STATE / "agent.jsonl", maxBytes=2 * 1024 * 1024, backupCount=3)
handler.setFormatter(logging.Formatter("%(message)s"))
log.addHandler(handler)


def audit(event, **fields):
    log.info(json.dumps({"time": time.time(), "event": event, **fields}))


websockets = {}
socket_expiries = {}
terminals = {}
confirmations = {}
events = collections.deque(maxlen=200)
event_seq = 0
files = HomeFiles()
process_cache = {}
last_snapshot = None
monitor_state = {}
last_warning = {}


async def emit(kind, data=None):
    global event_seq
    event_seq += 1
    e = {"id": event_seq, "kind": kind, "time": time.time(), "data": data or {}}
    events.append(e)
    audit(kind)
    asyncio.create_task(push.send(kind))


security.emit = emit
codex = CodexBridge(STATE, emit)
desktop_log = open(STATE / "desktop.log", "ab", buffering=0)
desktop = Desktop(STATE, desktop_log)


async def revoke(owner):
    push.revoke(owner)
    for ws in list(websockets.get(owner, set())):
        await ws.close(code=4001, message=b"Device revoked")
    for ident, t in list(terminals.items()):
        if t.owner == owner:
            await t.close()
            terminals.pop(ident, None)
    for thread, t in codex.threads.items():
        if t["owner"] == owner and t.get("status") in ("working", "waiting"):
            try:
                await codex.cancel(owner, thread)
            except Exception:
                pass


security.on_revoke = revoke

PUBLIC = {
    "/api/pair/options",
    "/api/pair/finish",
    "/api/login/options",
    "/api/login/finish",
    "/api/health",
}


@web.middleware
async def guard(req, handler):
    try:
        expected = urlparse(security.origin).netloc
        if req.host != expected:
            raise web.HTTPForbidden(text="Invalid host.")
        if req.headers.get("X-Carlos-API-Version", "1") != "1":
            raise web.HTTPUpgradeRequired(
                text="Unsupported Carlos API version. Update Carlos Mobile."
            )
        origin = req.headers.get("Origin")
        if req.path.startswith(("/api/", "/ws/")):
            if (req.method != "GET" or req.path.startswith("/ws/")) and origin != security.origin:
                raise web.HTTPForbidden(text="Invalid origin.")
            if origin and origin != security.origin:
                raise web.HTTPForbidden(text="Invalid origin.")
            if req.path not in PUBLIC:
                s = security.session(req)
                req["session"] = s
                if req.method not in ("GET", "HEAD") and not secrets.compare_digest(
                    req.headers.get("X-HoloHand-CSRF", ""), s["csrf"]
                ):
                    raise web.HTTPForbidden(text="Invalid CSRF token.")
            if req.method not in ("GET", "HEAD") and req.content_type != "application/json":
                raise web.HTTPUnsupportedMediaType(text="Use JSON.")
        resp = await handler(req)
    except web.HTTPException as e:
        resp = web.json_response({"error": e.text}, status=e.status)
    except (
        ValueError,
        KeyError,
        TypeError,
        FileNotFoundError,
        NotADirectoryError,
        IsADirectoryError,
        PermissionError,
    ) as e:
        resp = web.json_response({"error": str(e)[:500]}, status=400)
    except Exception as e:
        # Exception types, never request bodies, auth material or provider output.
        audit("request.error", type=type(e).__name__, path=req.path)
        resp = web.json_response({"error": f"{type(e).__name__}: {str(e)[:300]}"}, status=503)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Carlos-API-Version"] = "1"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    )
    if req.path.startswith("/api/") or req.path == "/" or req.path.endswith("sw.js"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


async def push_key(req):
    return web.json_response({"key": push.public})


async def push_subscribe(req):
    security.session(req, fresh=True)
    d = await req.json()
    push.subscribe(req["session"]["device"], d)
    return web.json_response({"ok": True})


async def health(req):
    return web.json_response(
        {"name": "Carlos Mobile", "online": True, "api_version": 1, "compatible_api_versions": [1]}
    )


async def me(req):
    s = req["session"]
    d = security.devices[s["device"]]
    return web.json_response(
        {
            "device": s["device"],
            "name": d["name"],
            "csrf": s["csrf"],
            "expires": s["expires"],
            "host": os.uname().nodename,
        }
    )


async def logout(req):
    security.sessions.pop(digest(req.cookies.get("hh_session", "")), None)
    r = web.json_response({"ok": True})
    r.del_cookie("hh_session")
    return r


async def devices(req):
    return web.json_response(
        [
            {"id": k, **{x: v for x, v in d.items() if x not in ("key", "credential", "counter")}}
            for k, d in security.devices.items()
        ]
    )


async def revoke_api(req):
    security.session(req, fresh=True)
    data = await req.json()
    require_confirmation(req, data, "revoke")
    await security.revoke(data["device"])
    await emit("device.revoked")
    return web.json_response({"ok": True})


async def confirmation(req):
    security.session(req, fresh=True)
    data = await req.json()
    if data.get("action") not in ("delete", "terminate", "kill", "revoke", "ev.restart"):
        raise ValueError("Unsupported confirmation.")
    now = time.time()
    for k, v in list(confirmations.items()):
        if v["expires"] < now:
            confirmations.pop(k, None)
    token = secrets.token_urlsafe(24)
    confirmations[token] = {
        "device": req["session"]["device"],
        "action": data["action"],
        "target": data.get("target"),
        "expires": now + 60,
    }
    return web.json_response({"confirmation": token, "expires": now + 60})


def require_confirmation(req, data, action):
    c = confirmations.pop(data.get("confirmation", ""), None)
    target = data.get("path", data.get("pid", data.get("device")))
    if (
        not c
        or c["device"] != req["session"]["device"]
        or c["expires"] < time.time()
        or c["action"] != action
        or c["target"] != target
    ):
        raise web.HTTPForbidden(text="Confirm this exact action again.")


async def system(req):
    return web.json_response(await asyncio.to_thread(system_snapshot))


async def processes(req):
    def collect():
        result = []
        for p in psutil.process_iter(
            ["pid", "name", "username", "memory_info", "create_time", "status"]
        ):
            try:
                d = p.info
                d["ram"] = d.pop("memory_info").rss
                d["cpu"] = p.cpu_percent()
                d["protected"] = (
                    d["username"] != os.environ.get("USER")
                    or d["name"]
                    in {
                        "kwin_wayland",
                        "plasmashell",
                        "pipewire",
                        "wireplumber",
                        "dbus-daemon",
                        "tailscaled",
                        "NetworkManager",
                        "ev-core",
                        "python3",
                        "python3.14",
                    }
                    or d["pid"] in (os.getpid(), os.getppid())
                )
                result.append(d)
            except (psutil.Error, AttributeError):
                pass
        return sorted(result, key=lambda p: p["ram"], reverse=True)

    return web.json_response(await asyncio.to_thread(collect))


async def process_signal(req):
    security.session(req, fresh=True)
    d = await req.json()
    action = "kill" if d.get("force") else "terminate"
    require_confirmation(req, d, action)
    p = psutil.Process(int(d["pid"]))
    if (
        p.username() != os.environ.get("USER")
        or p.name()
        in {
            "kwin_wayland",
            "plasmashell",
            "pipewire",
            "wireplumber",
            "dbus-daemon",
            "tailscaled",
            "NetworkManager",
            "python3",
            "python3.14",
            "ev-core",
        }
        or p.pid in (os.getpid(), os.getppid())
    ):
        raise web.HTTPForbidden(
            text="Protected process. Use the desktop for deliberate administration."
        )
    if abs(p.create_time() - float(d["created"])) > 0.01:
        raise ValueError("Process changed. Refresh the list.")
    p.kill() if d.get("force") else p.terminate()
    return web.json_response({"ok": True})


async def list_files(req):
    path = req.query.get("path", ".")
    rows = await asyncio.to_thread(files.listing, path)
    query = req.query.get("search", "").lower()
    return web.json_response(
        {"path": path, "entries": [r for r in rows if query in r["name"].lower()]}
    )


async def read_file(req):
    path = req.query["path"]
    data = await asyncio.to_thread(files.read, path)
    return web.json_response(
        {"path": path, "text": data.decode("utf-8"), "revision": hashlib.sha256(data).hexdigest()}
    )


async def download(req):
    path = req.query["path"]
    data = await asyncio.to_thread(files.read, path, 32 * 1024 * 1024)
    r = web.Response(body=data, content_type="application/octet-stream")
    from urllib.parse import quote

    r.headers["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(Path(path).name)
    r.headers["Cache-Control"] = "no-store"
    return r


async def image_file(req):
    path = req.query["path"]
    data = await asyncio.to_thread(files.read, path, 16 * 1024 * 1024)
    typ = mimetypes.guess_type(path)[0]
    if typ not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
        raise ValueError("Image format is not supported.")
    return web.Response(body=data, content_type=typ, headers={"Cache-Control": "no-store"})


async def save_file(req):
    security.session(req, fresh=True)
    d = await req.json()
    data = d["text"].encode() if "text" in d else base64.b64decode(d["base64"], validate=True)
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("Upload limit is 2 MiB.")
    await asyncio.to_thread(files.write, d["path"], data, d.get("revision"))
    await emit("file.saved")
    return web.json_response({"revision": hashlib.sha256(data).hexdigest()})


async def mutate_file(req):
    security.session(req, fresh=True)
    d = await req.json()
    if d["action"] == "delete":
        require_confirmation(req, d, "delete")
    await asyncio.to_thread(files.mutate, d["action"], d["path"], d.get("target"))
    return web.json_response({"ok": True})


async def terminal_list(req):
    return web.json_response(
        [
            {"id": t.id, "cwd": str(t.cwd), "running": t.proc.returncode is None}
            for t in terminals.values()
            if t.owner == req["session"]["device"]
        ]
    )


async def terminal_new(req):
    security.session(req, fresh=True)
    d = await req.json()
    owner = req["session"]["device"]
    if sum(t.owner == owner for t in terminals.values()) >= 4:
        raise ValueError("Close a terminal before opening another (limit 4).")
    t = Terminal(owner, homepath(d.get("cwd", ".")))
    await t.start()
    terminals[t.id] = t
    return web.json_response({"id": t.id})


async def terminal_close(req):
    d = await req.json()
    t = terminals[d["id"]]
    if t.owner != req["session"]["device"]:
        raise web.HTTPForbidden()
    await t.close()
    terminals.pop(t.id)
    return web.json_response({"ok": True})


async def socket_session(req, protocols=()):
    security.session(req, fresh=True)
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1024 * 1024, protocols=protocols)
    await ws.prepare(req)
    owner = req["session"]["device"]
    websockets.setdefault(owner, set()).add(ws)
    socket_expiries[ws] = req["session"]["expires"]
    return ws, owner


async def terminal_ws(req):
    t = terminals.get(req.match_info["id"])
    s = req["session"]
    if not t or t.owner != s["device"]:
        raise web.HTTPForbidden(text="Terminal is unavailable.")
    ws, owner = await socket_session(req)
    q = asyncio.Queue(maxsize=128)
    t.clients.add(q)
    since = int(req.query.get("since", "0"))
    for msg in t.history:
        if msg["seq"] > since:
            await ws.send_json(msg)

    async def output():
        while not ws.closed:
            await ws.send_json(await q.get())

    task = asyncio.create_task(output())
    try:
        async for msg in ws:
            security.session(req)
            t.last_seen = time.time()
            if msg.type == WSMsgType.TEXT:
                d = json.loads(msg.data)
                if d["type"] == "input":
                    t.write(base64.b64decode(d["data"], validate=True))
                elif d["type"] == "resize":
                    t.resize(int(d["cols"]), int(d["rows"]))
                else:
                    raise ValueError("Unknown terminal message.")
    finally:
        task.cancel()
        t.clients.discard(q)
        websockets[owner].discard(ws)
    return ws


async def monitor_list(req):
    return web.json_response(await desktop.monitors())


async def desktop_authorize(req):
    security.session(req, fresh=True)
    return web.json_response({"ok": True})


async def desktop_ws(req):
    monitor = int(req.query.get("monitor", "0"))
    quality = req.query.get("quality", "auto")
    if quality not in ("saver", "balanced", "ultra", "auto"):
        raise ValueError("Invalid quality.")
    security.session(req, fresh=True)
    reader, writer = await desktop.connect(monitor, quality)
    ws = None
    try:
        ws, owner = await socket_session(req, ("guacamole",))

        async def output():
            decoder = codecs.getincrementaldecoder("utf-8")()
            while data := await reader.read(65536):
                if desktop.active:
                    desktop.active["bytes"] += len(data)
                await ws.send_str(decoder.decode(data))
            await ws.close()

        task = asyncio.create_task(output())
        async for msg in ws:
            security.session(req)
            if msg.type == WSMsgType.TEXT:
                writer.write(msg.data.encode())
                await writer.drain()
        task.cancel()
    finally:
        writer.close()
        await writer.wait_closed()
        await desktop.release()
        if ws:
            websockets[owner].discard(ws)
    return ws


async def codex_list(req):
    owner = req["session"]["device"]
    return web.json_response(
        [{"id": k, **v} for k, v in codex.threads.items() if v["owner"] == owner]
    )


async def codex_action(req):
    security.session(req, fresh=True)
    d = await req.json()
    owner = req["session"]["device"]
    action = d["action"]
    if action == "start":
        r = await codex.create(owner, d.get("cwd", "."))
    elif action == "prompt":
        r = await codex.prompt(owner, d["thread"], str(d["text"]))
    elif action == "cancel":
        r = await codex.cancel(owner, d["thread"])
    elif action == "respond":
        r = await codex.respond(owner, d["requestId"], d.get("decision"), d.get("answers"))
    elif action == "read":
        codex.owned(d["thread"], owner)
        r = await codex.rpc("thread/read", {"threadId": d["thread"], "includeTurns": True})
    else:
        raise ValueError("Unknown Codex action.")
    return web.json_response(r)


async def codex_events(req):
    owner = req["session"]["device"]
    since = int(req.query.get("since", "0"))
    return web.json_response(
        [
            {**e, "requestId": e["requestId"] if str(e["requestId"]) in codex.approvals else None}
            for e in codex.events
            if e["seq"] > since and codex.threads.get(e["threadId"], {}).get("owner") == owner
        ]
    )


async def git_status(req):
    cwd = homepath(req.query.get("cwd", "."))
    status = await command("git", "status", "--short", cwd=cwd)
    diff = await command("git", "diff", "--no-ext-diff", "--no-textconv", "--", cwd=cwd)
    return web.json_response({"status": status, "diff": diff})


async def ev(req):
    try:
        return web.json_response(await ev_request("carlos.status"))
    except (OSError, TimeoutError):
        return web.json_response({"online": False, "error": "Carlos core is offline."})


async def ev_action(req):
    d = await req.json()
    action = d["action"]
    if action == "ask":
        r = await ev_request(
            "command.submit",
            {"text": str(d["text"])[:8000], "speak": d.get("phone_voice") is not True},
        )
    elif action == "events":
        r = await ev_request("events.history", {"limit": 30})
    elif action == "snapshot":
        r = await ev_request("snapshot")
    elif action == "privacy":
        security.session(req, fresh=True)
        r = await ev_request("carlos.privacy.set", {"mode": d.get("mode", "")})
    elif action == "transcribe":
        security.session(req, fresh=True)
        r = await ev_request("carlos.voice.transcribe", {"audio": d.get("audio", "")})
    elif action == "synthesize":
        security.session(req, fresh=True)
        r = await ev_request("carlos.voice.synthesize", {"text": d.get("text", "")})
    elif action == "confirm":
        security.session(req, fresh=True)
        r = await ev_request(
            "confirmation.respond",
            {
                "id": d["id"],
                "approval_token": d["approval_token"],
                "approved": d.get("approved") is True,
            },
        )
    elif action == "stop":
        r = await ev_request("command.submit", {"text": "stop everything"})
    elif action == "restart":
        security.session(req, fresh=True)
        require_confirmation(req, d, "ev.restart")
        try:
            await ev_request("core.stop")
        except OSError:
            pass
        r = await command(str(homepath(".local/bin/ev-activate")), timeout=20)
    else:
        raise ValueError("Unknown Carlos action.")
    return web.json_response(r)


async def action(req):
    security.session(req, fresh=True)
    d = await req.json()
    name = d["action"]
    commands = {
        "firefox": ["firefox"],
        "terminal": ["konsole"],
        "files": ["dolphin", str(Path.home())],
        "lock": ["qdbus6", "org.freedesktop.ScreenSaver", "/ScreenSaver", "Lock"],
        "holohand.status": [str(Path.home() / ".local/bin/holohand"), "--status"],
    }
    if name not in commands:
        raise ValueError("Unknown action.")
    if name in ("firefox", "terminal", "files"):
        p = await asyncio.create_subprocess_exec(
            *commands[name], stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        asyncio.create_task(p.wait())
        r = {"launched": True}
    else:
        r = await command(*commands[name])
    await emit("action." + name)
    return web.json_response(r)


async def chat(req):
    d = await req.json()
    text = str(d.get("text", "")).lower().strip()
    if any(s in text for s in ("what is my pc", "temperature", "how hot", "cpu", "ram")):
        return web.json_response({"system": await asyncio.to_thread(system_snapshot)})
    if "codex" in text:
        return web.json_response({"navigate": "codex"})
    if "desktop" in text:
        return web.json_response({"navigate": "desktop"})
    if text in ("open firefox", "lock my computer", "open terminal", "open files"):
        return web.json_response(
            {
                "suggestedAction": {
                    "open firefox": "firefox",
                    "lock my computer": "lock",
                    "open terminal": "terminal",
                    "open files": "files",
                }[text]
            }
        )
    if text == "restart ev":
        return web.json_response({"navigate": "ev", "message": "Use Restart Carlos and confirm."})
    try:
        return web.json_response(
            {"ev": await ev_request("command.submit", {"text": str(d["text"])[:8000]})}
        )
    except OSError:
        return web.json_response(
            {
                "message": "Carlos is offline. I can show system status, Codex, desktop, or offer application shortcuts."
            }
        )


async def event_list(req):
    return web.json_response(list(events))


async def diagnostics(req):
    tail = (
        await command("tailscale", "status", "--json")
        if Path("/usr/bin/tailscale").exists()
        else {"code": 1}
    )
    network = json.loads(tail["stdout"]) if tail["code"] == 0 else None
    return web.json_response(
        {
            "desktop": desktop.active,
            "tailscale": network,
            "sessions": len(terminals),
            "agent": {"pid": os.getpid(), "ram": psutil.Process().memory_info().rss},
            "origin": security.origin,
        }
    )


async def static(req):
    name = req.match_info.get("name", "") or "index.html"
    p = (ROOT / "dist" / name).resolve()
    if not p.is_relative_to(ROOT / "dist") or not p.is_file():
        raise web.HTTPNotFound()
    r = web.FileResponse(p)
    if name.startswith("assets/"):
        r.headers["Cache-Control"] = "public,max-age=31536000,immutable"
    return r


async def maintenance(app):
    async def loop():
        while True:
            await asyncio.sleep(20)
            now = time.time()
            ev_online = (
                Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "ev/ev.sock"
            ).exists()
            if monitor_state.get("ev", ev_online) != ev_online:
                await emit("ev.online" if ev_online else "ev.offline")
            monitor_state["ev"] = ev_online
            snapshot = await asyncio.to_thread(system_snapshot)
            for kind, condition in [
                ("system.temperature", any(t["celsius"] >= 90 for t in snapshot["temperatures"])),
                ("system.disk", snapshot["disk"]["free"] < 2 * 1024**3),
            ]:
                if condition and last_warning.get(kind, 0) < now - 1800:
                    last_warning[kind] = now
                    await emit(kind)
            security.sessions = {
                k: s
                for k, s in security.sessions.items()
                if s["expires"] > now and s["device"] in security.devices
            }
            live = {s["device"] for s in security.sessions.values()}
            for owner, sockets in list(websockets.items()):
                for ws in list(sockets):
                    if owner not in live or socket_expiries.get(ws, 0) < now:
                        await ws.close(code=4001, message=b"Authentication expired")
                        socket_expiries.pop(ws, None)
            for ident, t in list(terminals.items()):
                if not t.clients and now - t.last_seen > 1800:
                    await t.close()
                    terminals.pop(ident, None)

    task = asyncio.create_task(loop())
    await emit("agent.started")
    yield
    task.cancel()
    for sockets in websockets.values():
        for ws in list(sockets):
            await ws.close(code=1001, message=b"Agent restarting")
    for t in terminals.values():
        await t.close()
    await desktop.stop()
    await codex.close()
    desktop_log.close()


def create_app():
    app = web.Application(middlewares=[guard], client_max_size=3 * 1024 * 1024)
    routes = [
        web.get("/api/push/key", push_key),
        web.post("/api/push/subscribe", push_subscribe),
        web.get("/api/health", health),
        web.get("/api/me", me),
        web.post("/api/logout", logout),
        web.post("/api/pair/options", security.pair_options),
        web.post("/api/pair/finish", security.pair_finish),
        web.post("/api/login/options", security.login_options),
        web.post("/api/login/finish", security.login_finish),
        web.get("/api/devices", devices),
        web.post("/api/devices/revoke", revoke_api),
        web.post("/api/confirm", confirmation),
        web.get("/api/system", system),
        web.get("/api/processes", processes),
        web.post("/api/processes/signal", process_signal),
        web.get("/api/files", list_files),
        web.get("/api/files/read", read_file),
        web.get("/api/files/download", download),
        web.get("/api/files/image", image_file),
        web.post("/api/files/save", save_file),
        web.post("/api/files/action", mutate_file),
        web.get("/api/terminals", terminal_list),
        web.post("/api/terminals", terminal_new),
        web.post("/api/terminals/close", terminal_close),
        web.get("/ws/terminal/{id}", terminal_ws),
        web.get("/api/monitors", monitor_list),
        web.post("/api/desktop/authorize", desktop_authorize),
        web.get("/ws/desktop", desktop_ws),
        web.get("/api/codex", codex_list),
        web.post("/api/codex", codex_action),
        web.get("/api/codex/events", codex_events),
        web.get("/api/git", git_status),
        web.get("/api/ev", ev),
        web.post("/api/ev", ev_action),
        web.post("/api/action", action),
        web.post("/api/chat", chat),
        web.get("/api/events", event_list),
        web.get("/api/diagnostics", diagnostics),
        web.get("/{name:.*}", static),
    ]
    app.add_routes(routes)
    app.cleanup_ctx.append(maintenance)
    return app


if __name__ == "__main__":
    web.run_app(
        create_app(),
        host="127.0.0.1",
        port=int(os.environ.get("HOLOHAND_PORT", "8765")),
        access_log=None,
        print=None,
    )
