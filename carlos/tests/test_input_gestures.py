import asyncio
import threading
import time
import unittest
from unittest.mock import patch

from test_desktop_input import FakeDesktop, FakePortal
from ev.desktop.input import DesktopInput
from ev.tools.results import evaluate_result


class GestureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.desktop = FakeDesktop()
        self.portal = FakePortal(self.desktop)
        self.input = DesktopInput(self.desktop, self.portal)

    async def test_scoped_drag_moves_and_releases_without_claiming_drop_success(self):
        result = await self.input.gesture(
            "target",
            [
                {"type": "move", "x": 90, "y": 120},
                {"type": "button", "button": "left", "state": "press"},
                {"type": "move", "x": 190, "y": 220},
                {"type": "button", "button": "left", "state": "release"},
            ],
        )
        self.assertTrue(result["held_inputs_released"])
        self.assertFalse(result["verified"])
        self.assertEqual(self.portal.calls[-1], ("NotifyPointerButton", 272, 0))
        self.assertEqual(
            evaluate_result("desktop.input.gesture", result).status, "EXECUTED_UNVERIFIED"
        )

    async def test_modifier_hold_auto_released_at_end(self):
        await self.input.gesture(
            "target",
            [
                {"type": "key", "key": "ctrl", "state": "press"},
                {"type": "key", "key": "a", "state": "press"},
                {"type": "key", "key": "a", "state": "release"},
            ],
        )
        self.assertEqual(self.portal.calls[-1], ("NotifyKeyboardKeysym", 0xFFE3, 0))

    async def test_focus_change_during_hold_releases_without_more_input(self):
        self.portal.on_key = lambda args: self.desktop.world.update(active_window_id="other")
        with self.assertRaisesRegex(RuntimeError, "lost focus"):
            await self.input.gesture(
                "target",
                [
                    {"type": "key", "key": "ctrl", "state": "press"},
                    {"type": "key", "key": "a", "state": "press"},
                ],
            )
        self.assertEqual(
            self.portal.calls,
            [("NotifyKeyboardKeysym", 0xFFE3, 1), ("NotifyKeyboardKeysym", 0xFFE3, 0)],
        )

    async def test_invalid_later_event_refuses_entire_gesture(self):
        for event in (
            {"type": "unknown"},
            {"type": "pause", "milliseconds": 1000},
            {"type": "key", "key": "a", "state": "release"},
            {"type": "move", "x": float("nan"), "y": 20},
            {"type": "move", "x": 5000, "y": 5000},
        ):
            with self.subTest(event=event), self.assertRaises((ValueError, RuntimeError)):
                await self.input.gesture(
                    "target", [{"type": "key", "key": "ctrl", "state": "press"}, event]
                )
            self.assertFalse(self.portal.calls)

    async def test_relative_motion_has_independent_position_readback(self):
        result = await self.input.move_relative("target", 100, 50)
        self.assertEqual(result["cursor"], {"x": 130.0, "y": 90.0})
        self.assertTrue(evaluate_result("desktop.pointer.move_relative", result).verified)

    async def test_cancelled_press_finishes_before_release_not_after(self):
        entered, finish = threading.Event(), threading.Event()
        actual = self.portal.notify

        def blocked(method, *args):
            if args[-1] == 1:
                entered.set()
                finish.wait(2)
            actual(method, *args)

        self.portal.notify = blocked
        task = asyncio.create_task(self.input.key("target", "a"))
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        task.cancel()
        await asyncio.sleep(0.02)
        self.assertEqual(self.portal.calls, [])
        finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(
            self.portal.calls,
            [("NotifyKeyboardKeysym", ord("a"), 1), ("NotifyKeyboardKeysym", ord("a"), 0)],
        )

    async def test_release_failure_still_attempts_remaining_modifiers(self):
        actual = self.portal.notify

        def failing(method, code, state):
            actual(method, code, state)
            if code == ord("a") and state == 0:
                raise RuntimeError("release failed")

        self.portal.notify = failing
        with self.assertRaisesRegex(RuntimeError, "release failed"):
            await self.input.key("target", "a", ["ctrl", "shift"])
        self.assertEqual(
            self.portal.calls[-2:],
            [("NotifyKeyboardKeysym", 0xFFE1, 0), ("NotifyKeyboardKeysym", 0xFFE3, 0)],
        )

    async def test_media_and_standard_xkb_key_names(self):
        self.assertEqual(self.input._symbol("media_pause"), 0x1008FF31)
        self.assertEqual(self.input._symbol("KP_Enter"), 0xFF8D)
        self.assertEqual(self.input._symbol("é"), ord("é"))
        self.assertEqual(self.input._symbol("中"), 0x01000000 | ord("中"))

    async def test_power_keys_and_console_chords_cannot_bypass_system_route(self):
        for key in ("XF86PowerOff", "XF86Suspend", "XF86Switch_VT_1"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                await self.input.key("target", key)
        with self.assertRaises(ValueError):
            await self.input.key("target", "f2", ["ctrl", "alt"])
        with self.assertRaises(ValueError):
            await self.input.gesture(
                "target",
                [{"type": "key", "key": key, "state": "press"} for key in ("ctrl", "alt", "f2")],
            )
        self.assertEqual(self.portal.calls, [])

    async def test_connected_does_not_claim_functioning_without_observation(self):
        self.portal.status = lambda: {"connected": True, "available": True}
        self.assertIsNone(self.input.status()["functioning"])
        await self.input.move_relative("target", 10, 10)
        self.assertTrue(self.input.status()["functioning"])
        self.input._last_position_verified = time.monotonic() - 100
        self.assertIsNone(self.input.status()["functioning"])
