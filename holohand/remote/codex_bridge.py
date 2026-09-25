"""Supported Codex app-server JSON-RPC bridge, with explicit approval forwarding."""

import asyncio
import collections
import json
import time
from security import save
from host import homepath


class CodexBridge:
    def __init__(self, state, emit):
        self.state = state
        self.emit = emit
        self.proc = None
        self.seq = 0
        self.pending = {}
        self.approvals = {}
        self.lock = asyncio.Lock()
        self.events = collections.deque(maxlen=1200)
        self.event_seq = 0
        self.path = state / "codex-threads.json"
        self.threads = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.loaded = set()
        self.task = None

    async def start(self):
        async with self.lock:
            if self.proc and self.proc.returncode is None:
                return
            self.proc = await asyncio.create_subprocess_exec(
                str(homepath(".local/bin/codex")),
                "app-server",
                "--stdio",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=8 * 1024 * 1024,
            )
            self.loaded.clear()
            self.task = asyncio.create_task(self.reader())
            await self.rpc(
                "initialize",
                {"clientInfo": {"name": "holohand_mobile", "version": "0.1.0"}},
                start=False,
            )
            await self.send({"method": "initialized", "params": {}})

    async def send(self, data):
        self.proc.stdin.write((json.dumps(data) + "\n").encode())
        await self.proc.stdin.drain()

    async def rpc(self, method, params, start=True):
        if start:
            await self.start()
        self.seq += 1
        ident = self.seq
        fut = asyncio.get_running_loop().create_future()
        self.pending[ident] = fut
        try:
            await self.send({"id": ident, "method": method, "params": params})
            return await asyncio.wait_for(fut, 45)
        finally:
            self.pending.pop(ident, None)

    async def reader(self):
        try:
            while raw := await self.proc.stdout.readline():
                obj = json.loads(raw)
                if "id" in obj and ("result" in obj or "error" in obj):
                    f = self.pending.get(obj["id"])
                    if f and not f.done():
                        if "error" in obj:
                            f.set_exception(
                                RuntimeError(obj["error"].get("message", "Codex error"))
                            )
                        else:
                            f.set_result(obj["result"])
                    continue
                method = obj.get("method", "")
                params = obj.get("params", {})
                thread = params.get("threadId") or params.get("thread", {}).get("id")
                if thread not in self.threads:
                    continue
                if "id" in obj:
                    self.approvals[str(obj["id"])] = obj
                self.event_seq += 1
                event = {
                    "seq": self.event_seq,
                    "method": method,
                    "params": params,
                    "requestId": obj.get("id"),
                    "threadId": thread,
                }
                self.events.append(event)
                if method == "turn/started":
                    self.threads[thread]["turn"] = params["turn"]["id"]
                    self.threads[thread]["status"] = "working"
                if "requestApproval" in method or method.endswith("requestUserInput"):
                    self.threads[thread]["status"] = "waiting"
                    await self.emit("codex.waiting", {"threadId": thread})
                if method == "turn/completed":
                    self.threads[thread]["status"] = params["turn"]["status"]
                    await self.emit(
                        "codex.finished", {"threadId": thread, "status": params["turn"]["status"]}
                    )
                if (
                    method in ("turn/started", "turn/completed")
                    or "requestApproval" in method
                    or method.endswith("requestUserInput")
                ):
                    save(self.path, self.threads)
        except (OSError, ValueError, asyncio.CancelledError):
            pass
        finally:
            for f in self.pending.values():
                if not f.done():
                    f.set_exception(RuntimeError("Codex app server disconnected."))
            for t in self.threads.values():
                if t.get("status") in ("working", "waiting"):
                    t["status"] = "disconnected"

    def owned(self, thread, owner):
        if thread not in self.threads or self.threads[thread]["owner"] != owner:
            raise ValueError("This session is not owned by this device.")
        return self.threads[thread]

    async def create(self, owner, cwd):
        cwd = str(homepath(cwd))
        r = await self.rpc(
            "thread/start",
            {"cwd": cwd, "sandbox": "workspace-write", "approvalPolicy": "on-request"},
        )
        thread = r["thread"]["id"]
        self.threads[thread] = {
            "owner": owner,
            "cwd": cwd,
            "status": "ready",
            "created": time.time(),
        }
        self.loaded.add(thread)
        save(self.path, self.threads)
        return {"id": thread, **self.threads[thread]}

    async def prompt(self, owner, thread, text):
        t = self.owned(thread, owner)
        await self.start()
        if thread not in self.loaded:
            await self.rpc(
                "thread/resume",
                {
                    "threadId": thread,
                    "cwd": t["cwd"],
                    "sandbox": "workspace-write",
                    "approvalPolicy": "on-request",
                },
            )
            self.loaded.add(thread)
        return await self.rpc(
            "turn/start", {"threadId": thread, "input": [{"type": "text", "text": text[:64000]}]}
        )

    async def cancel(self, owner, thread):
        t = self.owned(thread, owner)
        return await self.rpc("turn/interrupt", {"threadId": thread, "turnId": t["turn"]})

    async def respond(self, owner, ident, decision, answers=None):
        request = self.approvals.get(str(ident))
        if not request:
            raise ValueError("Approval is no longer pending.")
        self.owned(request["params"]["threadId"], owner)
        method = request["method"]
        if method == "item/permissions/requestApproval":
            if decision not in ("accept", "decline"):
                raise ValueError("Invalid decision.")
            result = {
                "permissions": request["params"]["permissions"] if decision == "accept" else {},
                "scope": "turn",
            }
        elif method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            if decision not in ("accept", "decline", "cancel"):
                raise ValueError("Invalid decision.")
            result = {"decision": decision}
        elif method.endswith("/requestUserInput"):
            if not isinstance(answers, dict):
                raise ValueError("Answers are required.")
            expected = {q["id"] for q in request["params"]["questions"]}
            if set(answers) != expected or any(
                not isinstance(v, dict)
                or not isinstance(v.get("answers"), list)
                or not all(isinstance(a, str) and len(a) <= 8000 for a in v["answers"])
                for v in answers.values()
            ):
                raise ValueError("Provide an answer for each question.")
            result = {"answers": answers}
        else:
            raise ValueError("This request is not supported by the mobile bridge.")
        await self.send({"id": request["id"], "result": result})
        self.approvals.pop(str(ident), None)
        return {"ok": True}

    async def close(self):
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        if self.task:
            self.task.cancel()
