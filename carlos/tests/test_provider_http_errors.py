import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import web
from ev.ai.base import ProviderError
from ev.ai.nvidia import NvidiaProvider
from ev.ai.openai_responses import http_error_detail
from ev.brain import CommandEngine


class ProviderHttpErrorsTests(unittest.IsolatedAsyncioTestCase):
    def test_configured_key_is_not_live_connection_evidence(self):
        provider = NvidiaProvider({"model": "fixture", "api_key_env": "EV_TEST_HTTP_KEY"})
        engine = SimpleNamespace(provider=provider)
        with patch.dict(os.environ, {"EV_TEST_HTTP_KEY": "synthetic-only"}):
            status = CommandEngine.provider_status(engine)
            self.assertTrue(status["configured"])
            self.assertFalse(status["connected"])
            self.assertEqual(status["transport"]["state"], "UNTESTED")
            status["transport"]["state"] = "RESPONDED"
            self.assertEqual(provider.transport_status()["state"], "UNTESTED")

    def test_provider_error_shapes_are_bounded_and_redacted(self):
        for data in (
            {"error": "unavailable"},
            {"error": {"message": "unavailable"}},
            {"detail": "unavailable"},
            ["unavailable"],
            "unavailable",
            None,
        ):
            result = http_error_detail(json.dumps(data), "test-secret")
            self.assertIsInstance(result, str)
        result = http_error_detail(
            "test-secret nvapi-another-secret sk-another-secret " + "x" * 1000, "test-secret"
        )
        self.assertNotIn("secret", result)
        self.assertLessEqual(len(result), 500)

    async def test_real_http_error_then_plain_conversation_recovers_same_session(self):
        requests = []
        failing_status = 503

        async def endpoint(request):
            requests.append(await request.json())
            if len(requests) % 2 == 1:
                if failing_status == 404:
                    return web.Response(status=404)
                return web.json_response({"error": "temporary failure"}, status=503)
            return web.json_response(
                {"choices": [{"finish_reason": "stop", "message": {"content": "Hello again"}}]}
            )

        app = web.Application()
        app.router.add_post("/v1/chat/completions", endpoint)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        provider = NvidiaProvider(
            {
                "model": "fixture",
                "base_url": f"http://127.0.0.1:{port}/v1",
                "api_key_env": "EV_TEST_HTTP_KEY",
            }
        )
        try:
            with patch.dict(os.environ, {"EV_TEST_HTTP_KEY": "synthetic-only"}):
                for failing_status in (503, 404):
                    with self.subTest(http_status=failing_status):
                        before = len(requests)
                        detail = (
                            "temporary failure"
                            if failing_status == 503
                            else "No response detail supplied"
                        )
                        with self.assertRaisesRegex(
                            ProviderError,
                            f"Provider HTTP {failing_status}: {detail}.*endpoint /chat/completions",
                        ):
                            await provider.begin("Hello", [], [], [])
                        # Never blindly retry a failed cloud request or replay tools.
                        self.assertEqual(len(requests), before + 1)
                        status = CommandEngine.provider_status(SimpleNamespace(provider=provider))
                        self.assertFalse(status["connected"])
                        self.assertEqual(status["transport"]["http_status"], failing_status)
                        self.assertEqual(status["transport"]["endpoint"], "/chat/completions")
                        self.assertGreater(status["transport"]["observed_at"], 0)
                        self.assertNotIn("synthetic-only", json.dumps(status))
                        result = await provider.begin("Hello again", [], [], [])
                        self.assertEqual(result.text, "Hello again")
                        self.assertFalse(result.tool_calls)
                        self.assertTrue(
                            CommandEngine.provider_status(SimpleNamespace(provider=provider))[
                                "connected"
                            ]
                        )
                        self.assertEqual(provider.transport_status()["http_status"], 200)
                self.assertEqual(len(requests), 4)
        finally:
            await provider.close()
            await runner.cleanup()
