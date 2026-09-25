import unittest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from ev.web_lookup import fetch


class WebNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def page(self, html):
        with patch(
            "ev.web_lookup.download",
            new=AsyncMock(return_value=("https://example.com/docs/index.html", html)),
        ) as download:
            result = await fetch("https://example.com/start")
        download.assert_awaited_once_with("https://example.com/start")
        self.assertEqual(result["links_followed"], 0)
        self.assertTrue(result["untrusted_external_content"])
        return result

    async def test_observed_links_resolve_against_final_url_not_initial_or_html_base(self):
        result = await self.page(
            '<base href="http://localhost/"><a href="../guide?a=1&amp;b=2">Read <b>guide</b></a>'
        )
        self.assertEqual(
            result["links"], [{"url": "https://example.com/guide?a=1&b=2", "title": "Read guide"}]
        )
        self.assertFalse(result["links_truncated"])

    async def test_unsafe_hidden_and_anchor_only_links_are_not_navigation_candidates(self):
        body = "".join(
            f'<a href="{url}">bad</a>'
            for url in (
                "javascript:alert(1)",
                "file:///etc/passwd",
                "http://localhost/",
                "http://127.0.0.1/",
                "http://[::1]/",
                "https://user:pass@example.com/",
                "https://example.com:22/",
                "#section",
                "mailto:a@example.com",
            )
        )
        result = await self.page(
            body + '<script><a href="/hidden">bad</a></script><a href="/ok">OK</a>'
        )
        self.assertEqual(result["links"], [{"url": "https://example.com/ok", "title": "OK"}])

    async def test_links_are_bounded_deduplicated_and_truncation_is_explicit(self):
        body = '<a href="/same">first</a><a href="/same">duplicate</a>'
        body += "".join(f'<a href="/{i}">{"x" * 220}</a>' for i in range(45))
        result = await self.page(body)
        self.assertEqual(len(result["links"]), 40)
        self.assertEqual(result["links"][0]["title"], "first")
        self.assertTrue(result["links_truncated"])
        self.assertTrue(result["text_truncated"])
        self.assertTrue(all(len(link["title"]) <= 200 for link in result["links"]))

    async def test_unclosed_anchor_is_retained_but_not_followed(self):
        result = await self.page('<a href="/next">Next page')
        self.assertEqual(
            result["links"], [{"url": "https://example.com/next", "title": "Next page"}]
        )

    async def test_narrow_fetch_finds_link_beyond_first_forty_without_oversized_context(self):
        body = "<p>" + "x" * 7000 + "</p>"
        body += "".join(f'<a href="/{i}">Unrelated</a>' for i in range(80))
        body += '<a href="/guide">Calibration guide</a>'
        with patch(
            "ev.web_lookup.download", new=AsyncMock(return_value=("https://example.com/", body))
        ):
            result = await fetch(
                "https://example.com/", link_query="calibration", max_links=4, max_characters=100
            )
        self.assertEqual(
            result["links"], [{"url": "https://example.com/guide", "title": "Calibration guide"}]
        )
        self.assertTrue(result["links_filtered"])
        self.assertFalse(result["links_truncated"])
        self.assertTrue(result["text_truncated"])

    async def test_invalid_limits_rejected_before_network_access(self):
        for options in (
            {"max_links": 41},
            {"max_links": True},
            {"max_characters": 0},
            {"link_query": "x" * 101},
        ):
            with patch("ev.web_lookup.download", new=AsyncMock()) as download:
                with self.assertRaises(ValueError):
                    await fetch("https://example.com/", **options)
                download.assert_not_awaited()

    async def test_real_planner_returns_links_for_next_step_and_actual_page_evidence(self):
        from ev.ai.base import ProviderTurn, ToolCall
        from ev.ai.local_agent import LocalAgentProvider
        from ev.paths import Paths
        from ev.service import CarlosCore

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = CarlosCore(
                paths=Paths(*(root / n for n in ("config", "data", "state", "cache", "runtime")))
            )
            provider = LocalAgentProvider({})
            service.brain.provider = provider
            provider.begin = AsyncMock(
                return_value=ProviderTurn(
                    "fixture",
                    "test",
                    "",
                    [ToolCall("first", "web.fetch", {"url": "https://example.com/start"})],
                )
            )

            async def continuation(turn, outputs, tools):
                result = outputs[0][1]
                self.assertEqual(result["status"], "completed")
                self.assertIn("plan_id", result)
                page = result["result"]
                self.assertTrue(page["untrusted_external_content"])
                if outputs[0][0].call_id == "first":
                    return ProviderTurn(
                        "fixture",
                        "test",
                        "",
                        [ToolCall("second", "web.fetch", {"url": page["links"][0]["url"]})],
                    )
                return ProviderTurn("fixture", "test", page["sources"][0]["excerpt"])

            provider.continue_with_tools = continuation

            async def download(url):
                pages = {
                    "https://example.com/start": '<a href="/guide">Current guide</a>',
                    "https://example.com/guide": "<p>Calibration code: EV-42</p>",
                }
                return url, pages[url]

            try:
                with patch(
                    "ev.web_lookup.download", new=AsyncMock(side_effect=download)
                ) as transport:
                    result = await service._submit_action_clauses(
                        "Find the calibration code by following the guide from the documentation index",
                        "web-fixture",
                    )
                self.assertEqual(result["status"], "completed")
                self.assertIn("EV-42", result["response"])
                self.assertEqual(
                    [call.args[0] for call in transport.await_args_list],
                    ["https://example.com/start", "https://example.com/guide"],
                )
                self.assertEqual(
                    [p.steps[0].tool for p in service.planner.recent], ["web.fetch", "web.fetch"]
                )
            finally:
                service.memory.close()
