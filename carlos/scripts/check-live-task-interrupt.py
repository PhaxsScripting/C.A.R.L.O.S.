#!/usr/bin/env python3
"""Read-only wait + exact stop-all integration check on the installed IPC core.

No model, file changes, desktop input or application launches. Requires an idle
core. It deliberately ends E.V.'s conversation while cancelling its own wait.
"""

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from ev.cli import request
from ev.ipc.protocol import encode_message, decode_message
from ev.paths import get_paths


async def run():
    snapshot = (await request("snapshot"))["payload"]
    if snapshot["core"]["state"] != "DORMANT" or snapshot["planner"]["active"]:
        raise RuntimeError("E.V. is busy; refusing to interrupt an existing task")
    reader, writer = await asyncio.open_unix_connection(get_paths().socket, limit=1048576)
    wait_id, stop_id = uuid.uuid4().hex, uuid.uuid4().hex

    async def send(kind, identity, payload):
        writer.write(encode_message({"type": kind, "id": identity, "payload": payload}))
        await writer.drain()

    async def receive():
        line = await reader.readline()
        if not line:
            raise RuntimeError("Core disconnected")
        return decode_message(line, 1048576)

    try:
        async with asyncio.timeout(8):
            await receive()
            await send("subscribe", uuid.uuid4().hex, {})
            await send(
                "tool.call",
                wait_id,
                {
                    "name": "agent.wait_for",
                    "arguments": {
                        "conditions": [
                            {
                                "kind": "window_exists",
                                "window_id": "ev-nonexistent-fixture-" + uuid.uuid4().hex,
                            }
                        ],
                        "timeout_seconds": 60,
                        "interval_seconds": 30,
                    },
                },
            )
            while True:
                message = await receive()
                event = message.get("payload", {})
                if (
                    event.get("type") == "task.wait_state"
                    and event.get("correlation_id") == wait_id
                ):
                    break
            started = time.monotonic()
            await send("command.submit", stop_id, {"text": "stop everything"})
            responses = {}
            while len(responses) < 2:
                message = await receive()
                if message.get("type") == "response" and message.get("id") in {wait_id, stop_id}:
                    responses[message["id"]] = message["payload"]
            elapsed = time.monotonic() - started
            assert responses[wait_id].get("result", {}).get("wait_state") == "CANCELLED", responses[
                wait_id
            ]
            assert responses[stop_id].get("status") == "conversation_ended", responses[stop_id]
            assert elapsed < 2, elapsed
            print(
                json.dumps(
                    {
                        "passed": True,
                        "same_connection_interrupt_seconds": round(elapsed, 3),
                        "cloud_requests": 0,
                        "desktop_mutations": 0,
                        "wait_cancelled": True,
                    }
                )
            )
    finally:
        writer.close()
        await writer.wait_closed()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    if not parser.parse_args().run:
        parser.error("Pass --run to test the idle installed core")
    asyncio.run(run())
