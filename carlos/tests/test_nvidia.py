import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from ev.ai.base import ProviderError, ProviderTurn
from ev.ai.nvidia import NvidiaProvider
from ev.cloud_setup import setup_nvidia
from ev.config import load_config
from ev.paths import Paths


def reply(content=None, calls=None, finish="stop"):
    return {
        "choices": [{"finish_reason": finish, "message": {"content": content, "tool_calls": calls}}]
    }


def call(name, arguments, ident="call1"):
    return {
        "type": "function",
        "id": ident,
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


class NvidiaTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_transport_discovery_and_tool_continuation(self):
        provider = NvidiaProvider({"model": "nvidia/nemotron-3-super-120b-a12b"})
        provider.offline.begin = AsyncMock(return_value=ProviderTurn("offline", "test", ""))
        provider._request = AsyncMock(
            side_effect=[
                reply(
                    calls=[call("ev__load_tools", {"names": ["test.action"]})], finish="tool_calls"
                ),
                reply(
                    calls=[call("test__action", {"target": "exact"}, "call2")], finish="tool_calls"
                ),
                reply("Done."),
            ]
        )
        catalog = [
            {
                "name": "test.action",
                "description": "testing",
                "schema": {
                    "type": "object",
                    "properties": {"target": {"type": "string"}},
                    "required": ["target"],
                },
            }
        ]
        turn = await provider.begin("do it", [], [], catalog)
        self.assertEqual(turn.tool_calls[0].name, "test.action")
        result = await provider.continue_with_tools(
            turn, [(turn.tool_calls[0], {"ok": True})], catalog
        )
        self.assertEqual(result.text, "Done.")
        payload, endpoint = provider._request.await_args.args
        self.assertEqual(endpoint, "/chat/completions")
        self.assertEqual(payload["model"], "nvidia/nemotron-3-super-120b-a12b")
        self.assertFalse(payload["chat_template_kwargs"]["enable_thinking"])
        self.assertNotIn("input", payload)
        self.assertNotIn("store", payload)
        self.assertEqual(payload["messages"][-1]["tool_call_id"], "call2")
        self.assertEqual(
            payload["messages"][-2]["tool_calls"][0]["function"]["name"], "test__action"
        )
        self.assertIn("function", payload["tools"][0])

    async def test_even_direct_media_is_interpreted_by_nvidia(self):
        provider = NvidiaProvider({"model": "test"})
        provider.offline.begin = AsyncMock(
            side_effect=AssertionError("Local interpretation not requested")
        )
        provider._request = AsyncMock(return_value=reply("Please specify the device."))
        turn = await provider.begin("Unpause my Spotify, bro, what the fuck.", [], [], [])
        self.assertEqual(turn.provider, "nvidia")
        self.assertEqual(
            provider._request.await_args.args[0]["messages"][-1]["content"],
            "Unpause my Spotify, bro, what the fuck.",
        )
        provider.offline.begin.assert_not_awaited()

    async def test_service_uses_local_plan_then_preserves_unknown_cloud_request(self):
        from ev.service import CarlosCore

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = Paths(
                *(root / name for name in ("config", "data", "state", "cache", "runtime"))
            )
            service = CarlosCore(paths=paths)
            try:
                service.brain.provider = NvidiaProvider({"model": "test"})
                service.brain.submit = AsyncMock(return_value={"status": "completed"})
                service.planner.execute = AsyncMock(return_value={"status": "completed"})
                await service._submit_action_clauses("minimize my window")
                service.planner.execute.assert_awaited_once()
                service.brain.submit.assert_not_awaited()
                phrase = "get my coding workspace ready and find why the build failed"
                await service._submit_action_clauses(phrase)
                self.assertEqual(service.brain.submit.await_args.args[0], phrase)
                service.brain.submit.assert_awaited_once()
                service.voice.end_conversation = AsyncMock(return_value={"status": "completed"})
                await service._submit_action_clauses("stop talking")
                service.voice.end_conversation.assert_awaited_once()
                service.brain.submit.assert_awaited_once()
            finally:
                service.memory.close()

    async def test_bad_responses_fail_closed(self):
        provider = NvidiaProvider({"model": "test"})
        provider.offline.begin = AsyncMock(return_value=ProviderTurn("offline", "test", ""))
        for response in (
            {},
            reply("half", finish="length"),
            reply(calls=[call("invented", {})], finish="tool_calls"),
            reply(calls=[call("invented", {}, "")], finish="tool_calls"),
        ):
            provider._request = AsyncMock(return_value=response)
            with self.assertRaises(ProviderError):
                await provider.begin("a conversation", [], [], [])

    async def test_setup_private_and_failure_preserves_local_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = Paths(
                *(root / name for name in ("config", "data", "state", "cache", "runtime"))
            )
            load_config(paths)
            before = paths.config_file.read_bytes()
            key = "nvapi-unit-test-fixture"
            with patch("ev.cloud_setup.getpass.getpass", return_value=key), patch(
                "ev.cloud_setup.NvidiaProvider.begin",
                new=AsyncMock(side_effect=ProviderError("rejected")),
            ), patch("builtins.print"):
                with self.assertRaises(ProviderError):
                    await setup_nvidia(paths, "nvidia/nemotron-3-super-120b-a12b")
            self.assertEqual(paths.config_file.read_bytes(), before)
            self.assertFalse((paths.config_dir / "provider.env").exists())
            with patch("ev.cloud_setup.getpass.getpass", return_value=key), patch(
                "ev.cloud_setup.NvidiaProvider.begin",
                new=AsyncMock(return_value=ProviderTurn("nvidia", "test", "ready")),
            ), patch("builtins.print"):
                result = await setup_nvidia(paths, "nvidia/nemotron-3-super-120b-a12b")
            self.assertEqual(
                json.loads(paths.config_file.read_text())["providers"]["active"], "nvidia"
            )
            self.assertEqual((paths.config_dir / "provider.env").stat().st_mode & 0o777, 0o600)
            self.assertNotIn(key, json.dumps(result))
