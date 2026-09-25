from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from .ipc.protocol import decode_message, encode_message
from .paths import get_paths


async def request(message_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    socket_path = get_paths().socket
    reader, writer = await asyncio.open_unix_connection(socket_path, limit=1_048_576)
    await reader.readline()  # server hello
    request_id = uuid.uuid4().hex
    writer.write(encode_message({"type": message_type, "id": request_id, "payload": payload or {}}))
    await writer.drain()
    while line := await reader.readline():
        response = decode_message(line, 1_048_576)
        if response.get("id") == request_id or response["type"] == "error":
            writer.close()
            await writer.wait_closed()
            return response
    raise RuntimeError("Carlos core disconnected")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="evctl", description="Control the local Carlos core")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("health")
    cloud = commands.add_parser(
        "setup-openai",
        help="Privately enter and verify an OpenAI API key, then configure cloud conversation",
    )
    cloud.add_argument("--model", default="gpt-5.4-mini")
    nvidia = commands.add_parser(
        "setup-nvidia", help="Privately verify an NVIDIA API key and configure cloud conversation"
    )
    nvidia.add_argument("--model", default="nvidia/nemotron-3-super-120b-a12b")
    commands.add_parser("snapshot")
    ask = commands.add_parser("ask")
    ask.add_argument("text")
    commands.add_parser("listen")
    commands.add_parser("stop-listening")
    commands.add_parser("test-microphone")
    commands.add_parser("test-transcription")
    wake_test = commands.add_parser("test-wake")
    wake_test.add_argument(
        "--timeout-seconds",
        "--seconds",
        type=float,
        default=30.0,
        help="seconds to listen for Carlos (3-30; default: 30)",
    )
    commands.add_parser("test-full-voice")
    commands.add_parser("privacy-on")
    commands.add_parser("privacy-off")
    commands.add_parser("pause-wake")
    commands.add_parser("resume-wake")
    commands.add_parser("stop-speaking")
    speak = commands.add_parser("speak")
    speak.add_argument("text")
    history = commands.add_parser("events")
    history.add_argument("--limit", type=int, default=30)
    latency = commands.add_parser("latency")
    latency.add_argument("--limit", type=int, default=12)
    commands.add_parser("plans")
    history = commands.add_parser("agent-tasks", help="Inspect persistent agent task history")
    history.add_argument("--limit", type=int, default=20)
    detail = commands.add_parser("agent-task", help="Inspect one task's execution receipts")
    detail.add_argument("id")
    steer = commands.add_parser(
        "steer",
        help="Revise one exact task; observe fresh state and never reuse previous approvals",
    )
    steer.add_argument("id")
    steer.add_argument("text")
    commands.add_parser("security")
    commands.add_parser("diagnose")
    capabilities = commands.add_parser("capabilities")
    capabilities.add_argument("query", nargs="?", default="")
    personality = commands.add_parser("personality")
    personality.add_argument(
        "key",
        choices=(
            "response_length",
            "tone",
            "working_verbosity",
            "acknowledgements",
            "technical_language",
            "voice_expressiveness",
        ),
    )
    personality.add_argument("value")
    memories = commands.add_parser("memories")
    memories.add_argument("--query", default="")
    remember = commands.add_parser("remember")
    remember.add_argument("content")
    remember.add_argument("--tag", action="append", default=[])
    forget = commands.add_parser("forget")
    forget.add_argument("id")
    commands.add_parser("tools")
    tool = commands.add_parser("tool")
    tool.add_argument("name")
    tool.add_argument("--arguments", default="{}", help="JSON object")
    confirmations = commands.add_parser("confirmations")
    confirm = commands.add_parser("confirm")
    confirm.add_argument("id")
    confirm.add_argument("approval_token")
    confirm.add_argument("decision", choices=("approve", "deny"))
    commands.add_parser("stop")
    return root


async def async_main(arguments: argparse.Namespace) -> int:
    if arguments.command in {"setup-openai", "setup-nvidia"}:
        if not sys.stdin.isatty():
            print(
                f"Run evctl {arguments.command} in a local terminal so the API key can be entered privately.",
                file=sys.stderr,
            )
            return 1
        from .cloud_setup import setup_openai, setup_nvidia
        from .ai import ProviderError

        try:
            setup = setup_nvidia if arguments.command == "setup-nvidia" else setup_openai
            result = await setup(get_paths(), arguments.model)
        except (ValueError, OSError, ProviderError) as error:
            print(str(error), file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2))
        return 0
    personality_payload: dict[str, Any] = {}
    if arguments.command == "personality":
        value: Any = arguments.value
        if arguments.key == "voice_expressiveness":
            try:
                value = float(value)
            except ValueError as error:
                raise SystemExit(
                    "voice_expressiveness must be a number between 0.1 and 1.0"
                ) from error
        personality_payload = {arguments.key: value}
    mapping = {
        "steer": (
            "agent.tasks.steer",
            (
                {"task_id": arguments.id, "text": arguments.text}
                if arguments.command == "steer"
                else {}
            ),
        ),
        "health": ("health", {}),
        "snapshot": ("snapshot", {}),
        "ask": ("command.submit", {"text": arguments.text} if arguments.command == "ask" else {}),
        "listen": ("voice.capture.start", {}),
        "stop-listening": ("voice.capture.stop", {}),
        "test-microphone": ("voice.microphone_test.start", {}),
        "test-transcription": ("voice.transcription_test.start", {}),
        "test-wake": (
            "wake.test.start",
            (
                {"timeout_seconds": arguments.timeout_seconds}
                if arguments.command == "test-wake"
                else {}
            ),
        ),
        "test-full-voice": ("voice.full_test.start", {}),
        "privacy-on": ("voice.privacy.set", {"enabled": True}),
        "privacy-off": ("voice.privacy.set", {"enabled": False}),
        "pause-wake": ("wake.pause.set", {"paused": True}),
        "resume-wake": ("wake.pause.set", {"paused": False}),
        "stop-speaking": ("tts.stop", {}),
        "speak": ("tts.speak", {"text": arguments.text} if arguments.command == "speak" else {}),
        "events": (
            "events.history",
            {"limit": arguments.limit} if arguments.command == "events" else {},
        ),
        "latency": (
            "latency.report",
            {"limit": arguments.limit} if arguments.command == "latency" else {},
        ),
        "plans": ("plan.list", {}),
        "agent-tasks": (
            "agent.tasks.list",
            {"limit": arguments.limit} if arguments.command == "agent-tasks" else {},
        ),
        "agent-task": (
            "agent.tasks.get",
            {"id": arguments.id} if arguments.command == "agent-task" else {},
        ),
        "security": ("security.snapshot", {}),
        "diagnose": ("self.diagnostics", {}),
        "capabilities": (
            "capability.query",
            {"query": arguments.query} if arguments.command == "capabilities" else {},
        ),
        "personality": ("personality.update", personality_payload),
        "memories": (
            "memory.list",
            {"query": arguments.query} if arguments.command == "memories" else {},
        ),
        "remember": (
            "memory.remember",
            (
                {"content": arguments.content, "tags": arguments.tag}
                if arguments.command == "remember"
                else {}
            ),
        ),
        "forget": ("memory.forget", {"id": arguments.id} if arguments.command == "forget" else {}),
        "tools": ("tool.catalog", {}),
        "tool": (
            "tool.call",
            (
                {"name": arguments.name, "arguments": json.loads(arguments.arguments)}
                if arguments.command == "tool"
                else {}
            ),
        ),
        "confirmations": ("confirmation.list", {}),
        "confirm": (
            "confirmation.respond",
            (
                {
                    "id": arguments.id,
                    "approval_token": arguments.approval_token,
                    "approved": arguments.decision == "approve",
                }
                if arguments.command == "confirm"
                else {}
            ),
        ),
        "stop": ("core.stop", {}),
    }
    message_type, payload = mapping[arguments.command]
    try:
        response = await request(message_type, payload)
    except (ConnectionError, FileNotFoundError) as error:
        print(f"Carlos core is unavailable: {error}", file=sys.stderr)
        return 1
    print(json.dumps(response, indent=2, ensure_ascii=False))
    return 0 if response["type"] == "response" else 1


def main() -> int:
    return asyncio.run(async_main(parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
