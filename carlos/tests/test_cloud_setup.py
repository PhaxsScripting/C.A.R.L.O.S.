from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from ev.ai.base import ProviderError, ProviderTurn
from ev.ai.openai_responses import OpenAIResponsesProvider, strict_schema
from ev.cloud_setup import setup_openai
from ev.config import load_config
from ev.paths import Paths


def cloud_tool(name, description):
    return {
        "name": name,
        "description": description,
        "schema": {
            "type": "object",
            "properties": {"target": {"type": "string"}},
            "required": ["target"],
            "additionalProperties": False,
        },
    }


class CloudProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_media_command_never_calls_cloud_even_without_key(self):
        provider = OpenAIResponsesProvider({"model": "test"})
        provider._post = AsyncMock(side_effect=AssertionError("unnecessary network call"))
        result = await provider.begin("Unpause my Spotify, bro, what the fuck.", [], [], [])
        self.assertEqual(result.tool_calls[0].name, "audio.media")
        self.assertEqual(result.tool_calls[0].arguments, {"action": "play", "player": "spotify"})
        answer = await provider.continue_with_tools(
            result,
            [
                (
                    result.tool_calls[0],
                    {"result": {"ok": True, "verified": True, "message": "Spotify is playing."}},
                )
            ],
            [],
        )
        self.assertEqual(answer.text, "Spotify is playing.")
        provider._post.assert_not_awaited()

    async def test_discovers_missing_tool_and_returns_only_registered_call(self):
        provider = OpenAIResponsesProvider({"model": "test", "reasoning_effort": "none"})
        provider.offline.begin = AsyncMock(return_value=ProviderTurn("offline", "test", ""))
        provider._post = AsyncMock(
            side_effect=[
                {
                    "output": [
                        {
                            "type": "function_call",
                            "name": "ev__load_tools",
                            "call_id": "load-1",
                            "arguments": '{"names":["settings.something"]}',
                        }
                    ]
                },
                {
                    "output": [
                        {
                            "type": "function_call",
                            "name": "settings__something",
                            "call_id": "do-1",
                            "arguments": '{"target":"exact"}',
                        }
                    ]
                },
            ]
        )
        catalog = [
            cloud_tool(f"feature.item{index}", "generic capability") for index in range(169)
        ] + [cloud_tool("settings.something", "Special native setting")]
        result = await provider.begin("adjust it", [], [], catalog)
        self.assertEqual(result.tool_calls[0].name, "settings.something")
        payload = provider._post.await_args_list[1].args[0]
        self.assertFalse(payload["store"])
        self.assertFalse(payload["parallel_tool_calls"])
        self.assertEqual(payload["reasoning"], {"effort": "none"})
        self.assertEqual(len(payload["tools"]), 2)
        self.assertEqual(payload["input"][-1]["call_id"], "load-1")

    async def test_unknown_or_incomplete_cloud_call_is_not_executed(self):
        provider = OpenAIResponsesProvider({"model": "test"})
        for response in (
            {"output": [{"type": "function_call", "name": "invented", "arguments": "{}"}]},
            {"status": "incomplete", "output": []},
        ):
            with self.subTest(response=response), self.assertRaises(ProviderError):
                provider._parse(response, [], {}, 0)

    async def test_cloud_discovery_has_an_overall_deadline(self):
        provider = OpenAIResponsesProvider({"model": "test", "turn_timeout_seconds": 0.01})
        provider._post = AsyncMock(side_effect=lambda *_: None)

        async def stalled(_payload):
            await asyncio.sleep(60)

        provider._post = stalled
        with self.assertRaisesRegex(ProviderError, "timed out"):
            await provider._respond([], [], [], 0)

    def test_optional_enums_accept_null_in_strict_schema(self):
        result = strict_schema(
            {"type": "object", "properties": {"mode": {"type": "string", "enum": ["on", "off"]}}}
        )
        self.assertIn(None, result["properties"]["mode"]["enum"])


class PrivateSetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_key_is_private_configured_only_after_validated_and_other_keys_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = Paths(
                *(root / name for name in ("config", "data", "state", "cache", "runtime"))
            )
            load_config(paths)
            credentials = paths.config_dir / "provider.env"
            credentials.write_text("GROQ_API_KEY=existing-value\n", encoding="utf-8")
            credentials.chmod(0o600)
            with patch(
                "ev.cloud_setup.getpass.getpass", return_value="sk-test-not-a-real-key-1234567890"
            ), patch(
                "ev.cloud_setup.OpenAIResponsesProvider.begin",
                new=AsyncMock(return_value=ProviderTurn("cloud", "test", "ready")),
            ), patch(
                "builtins.print"
            ):
                result = await setup_openai(paths, "gpt-5.4-mini")
            self.assertEqual(credentials.stat().st_mode & 0o777, 0o600)
            self.assertEqual(paths.config_file.stat().st_mode & 0o777, 0o600)
            self.assertIn("GROQ_API_KEY=existing-value", credentials.read_text())
            self.assertEqual(
                json.loads(paths.config_file.read_text())["providers"]["active"],
                "openai_compatible",
            )
            self.assertNotIn("sk-test", json.dumps(result))

    async def test_failed_api_check_preserves_working_provider_and_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = Paths(
                *(root / name for name in ("config", "data", "state", "cache", "runtime"))
            )
            load_config(paths)
            previous = paths.config_file.read_bytes()
            with patch(
                "ev.cloud_setup.getpass.getpass", return_value="sk-test-not-a-real-key-1234567890"
            ), patch(
                "ev.cloud_setup.OpenAIResponsesProvider.begin",
                new=AsyncMock(side_effect=ProviderError("rejected")),
            ), patch(
                "builtins.print"
            ), self.assertRaises(
                ProviderError
            ):
                await setup_openai(paths, "gpt-5.4-mini")
            self.assertEqual(paths.config_file.read_bytes(), previous)
            self.assertFalse((paths.config_dir / "provider.env").exists())
