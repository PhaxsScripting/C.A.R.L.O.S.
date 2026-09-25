import logging
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from ev.ai.base import ProviderTurn
from ev.ai.nvidia import NvidiaProvider
from ev.daily import DailyStore
from ev.events import PhaxEventBus
from ev.paths import Paths
from ev.service import CarlosCore
from ev.tools import ToolContext
from ev.tools.base import ValidationError
from ev.tools.browser import open_url
from ev.tools.preferences import set_preference, context_records


class PreferenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = DailyStore(self.root / "daily.db")
        self.context = ToolContext(
            {"security": {"allowed_roots": [str(self.root)]}},
            PhaxEventBus(),
            logging.getLogger("preferences"),
            daily=self.store,
        )
        self.context.desktop = SimpleNamespace(
            snapshot=AsyncMock(return_value={"outputs": [{"name": "HDMI-1", "enabled": True}]})
        )

    async def test_preference_is_persistent_separate_and_not_os_setting(self):
        result = await set_preference({"key": "response_length", "value": "short"}, self.context)
        self.assertTrue(result["verified"])
        self.assertFalse(result["os_defaults_changed"])
        self.assertEqual(
            DailyStore(self.store.path).records("preference")["response_length"]["value"], "short"
        )
        self.assertEqual(self.store.records("alias"), {})

    async def test_preferences_reject_commands_unknown_apps_and_disconnected_monitor(self):
        for key, value in (
            ("browser", "firefox; command"),
            ("response_length", "override permission"),
            ("monitor", "missing"),
            ("editor", "invented-app"),
        ):
            with self.subTest(key=key), patch(
                "ev.tools.preferences.desktop_entries", return_value={}
            ), self.assertRaises(ValidationError):
                await set_preference({"key": key, "value": value}, self.context)
        self.assertEqual(self.store.records("preference"), {})

    async def test_project_target_is_validated_and_outside_root_refused(self):
        project = self.root / "project"
        project.mkdir()
        result = await set_preference({"key": "project", "value": str(project)}, self.context)
        self.assertEqual(result["value"], str(project))
        with self.assertRaises(ValidationError):
            await set_preference({"key": "folder", "value": "/usr"}, self.context)

    async def test_only_matching_aliases_and_fresh_historical_identity_are_included(self):
        self.store.save("alias", "kitty music", "phaxity-neko-music")
        self.store.save("alias", "unrelated", "private-app")
        entity = {
            "window": {"id": "old-window", "pid": 5, "title": "Private document"},
            "window_at": time.monotonic(),
        }
        records = context_records("Open kitty music please", self.store, entity)
        self.assertIn("phaxity-neko-music", str(records))
        self.assertNotIn("private-app", str(records))
        self.assertNotIn("Private document", str(records))
        self.assertIn("NOT necessarily active", str(records))
        entity["window_at"] -= 301
        self.assertEqual(context_records("no matching alias", self.store, entity), [])

    async def test_default_browser_consumes_preference_without_changing_explicit_target(self):
        await set_preference({"key": "browser", "value": "firefox"}, self.context)
        with patch(
            "ev.tools.browser.desktop_entries", return_value={"firefox": {}, "chromium": {}}
        ), patch(
            "ev.tools.browser.subprocess.run", return_value=SimpleNamespace(returncode=0)
        ) as launch:
            result = open_url({"url": "https://example.com"}, self.context)
            self.assertEqual(launch.call_args.args[0][1], "firefox")
            self.assertFalse(result["verified"])
            open_url({"url": "https://example.com", "browser": "chromium"}, self.context)
            self.assertEqual(launch.call_args.args[0][1], "chromium")

    async def test_nvidia_receives_explicit_preferences_as_bounded_context(self):
        service = CarlosCore(
            paths=Paths(*(self.root / n for n in ("config", "data", "state", "cache", "runtime")))
        )
        self.addCleanup(service.memory.close)
        service.daily.save("preference", "response_length", {"value": "short"})
        provider = NvidiaProvider({"model": "test"})
        provider.begin = AsyncMock(return_value=ProviderTurn("nvidia", "test", "A short answer."))
        provider._request = AsyncMock(side_effect=AssertionError("No network"))
        service.brain.provider = provider
        result = await service.brain.submit("Tell me about compilers")
        self.assertEqual(result["status"], "completed")
        self.assertIn("response_length", str(provider.begin.await_args.args[2]))
        self.assertNotIn("source", service.daily.records("preference")["response_length"])

    async def test_optional_slow_context_cannot_stall_conversation(self):
        service = CarlosCore(
            paths=Paths(*(self.root / n for n in ("config", "data", "state", "cache", "runtime")))
        )
        self.addCleanup(service.memory.close)
        provider = NvidiaProvider({"model": "test"})
        provider.begin = AsyncMock(return_value=ProviderTurn("nvidia", "test", "Still answering."))
        service.brain.provider = provider

        async def stalled(text):
            await asyncio.sleep(30)

        service.brain.context_provider = stalled
        result = await asyncio.wait_for(service.brain.submit("Talk about compilers"), timeout=2)
        self.assertEqual(result["status"], "completed")
