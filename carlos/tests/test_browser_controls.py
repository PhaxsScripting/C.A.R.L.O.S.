from __future__ import annotations
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from ev.commands import direct_action
from ev.tools.browser import video_control, browser_scope, browser_shortcut, SHORTCUTS


class BrowserControlsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.window = {
            "id": "exact",
            "pid": 123,
            "title": "Video - YouTube",
            "app_id": "firefox-bin",
        }
        self.world = {"active_window_id": "exact", "windows": [self.window]}
        self.label = "Full screen (f)"
        self.accessibility = SimpleNamespace(
            list_elements=MagicMock(side_effect=self.listing),
            activate=MagicMock(side_effect=self.activate),
        )
        self.context = SimpleNamespace(
            accessibility=self.accessibility,
            desktop=SimpleNamespace(
                snapshot=AsyncMock(side_effect=lambda **kw: self.world),
                bridge=SimpleNamespace(request=AsyncMock(side_effect=lambda *a, **kw: self.world)),
                input=SimpleNamespace(
                    status=lambda: {"connected": True},
                    key=AsyncMock(return_value={"input_sent": True, "verified": True}),
                ),
            ),
        )

    def listing(self, *args):
        return {
            "elements": [{"name": self.label, "role": "button", "application": "Firefox"}],
            "truncated": False,
        }

    def activate(self, *args):
        self.label = "Exit full screen (f)"
        return {"action_accepted": True}

    async def test_video_state_is_verified_and_repeated_request_is_idempotent(self):
        args = {"window_id": "exact", "action": "fullscreen"}
        result = await video_control(args, self.context)
        self.assertTrue(result["verified"])
        result = await video_control(args, self.context)
        self.assertTrue(result["already_set"])
        self.accessibility.activate.assert_called_once()

    async def test_ambiguous_or_truncated_control_search_never_clicks(self):
        for listing in (
            {"elements": [{"name": "Full screen"}, {"name": "Full screen"}], "truncated": False},
            {"elements": [], "truncated": True},
        ):
            self.accessibility.list_elements.side_effect = None
            self.accessibility.list_elements.return_value = listing
            with self.assertRaises(RuntimeError):
                await video_control({"window_id": "exact", "action": "fullscreen"}, self.context)
        self.accessibility.activate.assert_not_called()

    async def test_focus_loss_or_nonbrowser_target_never_receives_input(self):
        self.world["active_window_id"] = "other"
        with self.assertRaisesRegex(RuntimeError, "focus"):
            await browser_scope({"window_id": "exact"}, self.context)
        self.world["active_window_id"] = "exact"
        self.window["app_id"] = "code"
        with self.assertRaisesRegex(RuntimeError, "recognized browser"):
            await browser_shortcut({"window_id": "exact", "action": "new_tab"}, self.context)
        self.context.desktop.input.key.assert_not_awaited()

    async def test_shortcut_delivery_is_not_mislabeled_as_page_verification(self):
        result = await browser_shortcut({"window_id": "exact", "action": "zoom_in"}, self.context)
        self.assertTrue(result["input_sent"])
        self.assertFalse(result["verified"])
        self.context.desktop.input.key.assert_awaited_once_with("exact", "+", ["ctrl"])

    def test_video_requests_do_not_fullscreen_browser_chrome(self):
        for text in (
            "fullscreen my YouTube video",
            "make my youtube video fullscreen",
            "full screen the video",
        ):
            action = direct_action(text)
            self.assertEqual(action.tool, "browser.video")
            self.assertEqual(action.arguments["action"], "fullscreen")
        self.assertEqual(direct_action("fullscreen Firefox").tool, "window.state")
        for text in (
            "How do I fullscreen my YouTube video?",
            "don't fullscreen YouTube",
            "I was watching YouTube fullscreen",
        ):
            self.assertIsNone(direct_action(text))

    def test_browser_shortcut_keys_are_supported(self):
        from ev.desktop.input import DesktopInput

        for key, modifiers in SHORTCUTS.values():
            self.assertTrue(len(key) == 1 or key in DesktopInput.KEYS)
