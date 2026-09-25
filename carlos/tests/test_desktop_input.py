from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from ev.desktop.input import DesktopInput, RemoteDesktopPortal


class FakeDesktop:
    def __init__(self) -> None:
        self.bridge = self
        self.world = {
            "active_window_id": "target",
            "current_desktop": "main",
            "cursor": {"x": 30, "y": 40},
            "outputs": [{"geometry": {"x": 0, "y": 0, "width": 1920, "height": 1080}}],
            "windows": [
                {
                    "id": "target",
                    "normal": True,
                    "stacking_order": 5,
                    "geometry": {"x": 20, "y": 20, "width": 800, "height": 600},
                }
            ],
        }

    async def request(self, action, arguments, timeout=4):
        return deepcopy(self.world)


class FakePortal:
    def __init__(self, desktop) -> None:
        self.desktop = desktop
        self.calls = []
        self.move_pointer = True
        self.on_key = None
        self.fail_symbol = None

    def notify(self, method, *args):
        self.calls.append((method, *args))
        if method == "NotifyPointerMotion" and self.move_pointer:
            self.desktop.world["cursor"]["x"] += args[0]
            self.desktop.world["cursor"]["y"] += args[1]
        if method == "NotifyKeyboardKeysym":
            if self.on_key:
                self.on_key(args)
            if args == (self.fail_symbol, 1):
                raise RuntimeError("key send failed")


class DesktopInputTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.desktop = FakeDesktop()
        self.portal = FakePortal(self.desktop)
        self.input = DesktopInput(self.desktop, self.portal)

    async def test_pointer_move_verifies_live_cursor(self):
        result = await self.input.move("target", 90, 120)
        self.assertTrue(result["verified"])
        self.assertEqual(result["cursor"], {"x": 90.0, "y": 120.0})
        self.assertEqual(self.portal.calls, [("NotifyPointerMotion", 60.0, 80.0)])

    async def test_control_guard_stops_later_chunks_without_retyping_or_held_keys(self):
        offsets = []

        async def guard(offset):
            offsets.append(offset)
            if offset == 8:
                raise RuntimeError("Field changed")

        with self.assertRaisesRegex(RuntimeError, "Field changed"):
            await self.input.type_text("target", "abcdefghijkl", control_guard=guard)
        self.assertEqual(offsets, [0, 8])
        self.assertEqual(len(self.portal.calls), 16)
        self.assertEqual(self.portal.calls[-1], ("NotifyKeyboardKeysym", ord("h"), 0))

    async def test_click_is_not_sent_when_pointer_backend_does_not_move(self):
        self.portal.move_pointer = False
        with self.assertRaisesRegex(RuntimeError, "Pointer did not reach"):
            await self.input.click("target", 90, 120)
        self.assertFalse(any(call[0] == "NotifyPointerButton" for call in self.portal.calls))

    async def test_pointer_input_rejects_other_window_and_covered_coordinates(self):
        self.desktop.world["active_window_id"] = "other"
        with self.assertRaisesRegex(RuntimeError, "lost focus"):
            await self.input.click("target", 90, 120)
        self.desktop.world["active_window_id"] = "target"
        self.desktop.world["windows"].append(
            {
                "id": "overlay",
                "stacking_order": 8,
                "geometry": {"x": 80, "y": 80, "width": 200, "height": 200},
            }
        )
        with self.assertRaisesRegex(RuntimeError, "covers"):
            await self.input.click("target", 90, 120)
        self.assertEqual(self.portal.calls, [])

    async def test_outside_window_coordinates_do_not_send_input(self):
        with self.assertRaisesRegex(RuntimeError, "outside the exact"):
            await self.input.click("target", 1000, 900)
        self.assertEqual(self.portal.calls, [])

    async def test_click_releases_button_and_does_not_claim_application_success(self):
        result = await self.input.click("target", 90, 120)
        self.assertTrue(result["input_sent"])
        self.assertFalse(result["verified"])
        self.assertEqual(
            self.portal.calls[-2:],
            [("NotifyPointerButton", 272, 1), ("NotifyPointerButton", 272, 0)],
        )

    async def test_shortcut_releases_all_modifiers_after_failed_key_send(self):
        self.portal.fail_symbol = ord("l")
        with self.assertRaisesRegex(RuntimeError, "key send failed"):
            await self.input.key("target", "l", ["ctrl"])
        self.assertEqual(
            self.portal.calls[-2:],
            [("NotifyKeyboardKeysym", ord("l"), 0), ("NotifyKeyboardKeysym", 0xFFE3, 0)],
        )

    async def test_text_refuses_submission_characters_before_input(self):
        with self.assertRaisesRegex(ValueError, "printable"):
            await self.input.type_text("target", "erase something\n")
        self.assertEqual(self.portal.calls, [])

    async def test_typing_stops_when_user_changes_focus_between_chunks(self):
        self.portal.on_key = lambda args: (
            self.desktop.world.update(active_window_id="other") if args == (ord("h"), 0) else None
        )
        with self.assertRaisesRegex(RuntimeError, "lost focus"):
            await self.input.type_text("target", "abcdefghijklmnop")
        pressed = [call[1] for call in self.portal.calls if call[2] == 1]
        self.assertEqual(pressed, list(map(ord, "abcdefgh")))

    async def test_scroll_direction_is_correct_and_bounded(self):
        await self.input.scroll("target", 90, 120, "up", 4)
        self.assertEqual(self.portal.calls[-1], ("NotifyPointerAxisDiscrete", 0, -4))
        with self.assertRaises(ValueError):
            await self.input.scroll("target", 90, 120, "down", 1000)

    async def test_scroll_rechecks_occlusion_immediately_before_delivery(self):
        self.desktop.world["cursor"] = {"x": 90, "y": 120}
        calls = 0

        async def changing_world(action, arguments, timeout=4):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.desktop.world["windows"].append(
                    {
                        "id": "late-overlay",
                        "stacking_order": 9,
                        "geometry": {"x": 80, "y": 80, "width": 200, "height": 200},
                    }
                )
            return deepcopy(self.desktop.world)

        self.desktop.request = changing_world
        with self.assertRaisesRegex(RuntimeError, "covers"):
            await self.input.scroll("target", 90, 120, "down", 2)
        self.assertFalse(any(call[0] == "NotifyPointerAxisDiscrete" for call in self.portal.calls))

    def test_notify_failure_closes_the_still_addressable_portal_session(self):
        class FailingRemote:
            def NotifyPointerMotion(self, *_args, **_kwargs):
                raise TimeoutError("reply lost")

        portal = RemoteDesktopPortal()
        portal._session = "/org/freedesktop/portal/desktop/session/test/session"
        portal._devices = 2
        portal._remote = FailingRemote()
        with patch.object(portal, "close", return_value={"verified": True}) as close:
            with self.assertRaisesRegex(TimeoutError, "reply lost"):
                portal.notify("NotifyPointerMotion", 1.0, 2.0)
        close.assert_called_once_with()

    def test_restore_token_is_private_and_never_in_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "input.json"
            portal = RemoteDesktopPortal(path)
            portal._save_restore_token("private-session-token")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(portal._load_restore_token(), "private-session-token")
            self.assertNotIn("private-session-token", str(portal.status()))
            portal._save_restore_token("")
            self.assertEqual(portal._load_restore_token(), "")


if __name__ == "__main__":
    unittest.main()
