import unittest
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock, patch

from ev.accessibility import AccessibilityBridge
from ev.goals import verify_conditions
from ev.tools.controls import control_text, control_type_empty

IDENTITY = {
    "window_id": "window",
    "process_id": 123,
    "window_title": "Fixture",
    "application": "Fixture",
    "name": "Search",
    "role": "entry",
    "path": "/exact",
}


class ControlTextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bridge = AccessibilityBridge.__new__(AccessibilityBridge)
        self.node = Mock()
        self.node.get_character_count.return_value = 3
        self.node.is_editable_text.return_value = True
        self.before = {
            "path": "/exact",
            "role": "entry",
            "states": {"enabled": True, "sensitive": True},
        }
        self.bridge._resolve_node = Mock(return_value=(self.node, self.before))
        self.bridge.Atspi = SimpleNamespace(Text=SimpleNamespace(get_text=Mock(return_value="old")))
        self.bridge.inspect_control = Mock(
            return_value={"text_available": True, "text_truncated": False, "text": "new"}
        )

    def write(self):
        return self.bridge.replace_control_text(
            "Fixture", "Search", "entry", 123, "Fixture", "/exact", "old", "new"
        )

    def test_exact_compare_write_readback_never_submits(self):
        result = self.write()
        self.assertTrue(result["verified"])
        self.assertFalse(result["submitted"])
        self.node.set_text_contents.assert_called_once_with("new")
        self.node.do_action.assert_not_called()

    def test_stale_text_and_passwords_never_written(self):
        self.bridge.Atspi.Text.get_text.return_value = "user changed it"
        with self.assertRaisesRegex(RuntimeError, "changed"):
            self.write()
        self.before["role"] = "password text"
        with self.assertRaisesRegex(RuntimeError, "non-password"):
            self.write()
        self.node.set_text_contents.assert_not_called()

    def test_partial_or_missing_readback_is_not_success(self):
        for after in (
            {},
            {"text_available": True, "text_truncated": True, "text": "new"},
            {"text_available": True, "text": "wrong"},
        ):
            self.bridge.inspect_control.return_value = after
            self.assertFalse(self.write()["verified"])

    def test_disabled_stale_path_and_oversized_text_refused(self):
        for mutation in (
            lambda: self.before.update(path="/changed"),
            lambda: self.before["states"].update(enabled=False),
            lambda: setattr(self.node.get_character_count, "return_value", 2001),
        ):
            self.setUp()
            mutation()
            with self.assertRaises(RuntimeError):
                self.write()
            self.node.set_text_contents.assert_not_called()

    async def test_focus_change_reports_uncertain_not_verified(self):
        context = SimpleNamespace(
            accessibility=SimpleNamespace(
                replace_control_text=Mock(return_value={"verified": True})
            )
        )
        with patch(
            "ev.tools.controls._active_accessibility_scope",
            AsyncMock(side_effect=[(123, "Fixture"), (456, "Other")]),
        ):
            result = await control_text(
                {**IDENTITY, "expected_text": "old", "text": "new"}, context
            )
        self.assertFalse(result["verified"])
        self.assertFalse(result["submitted"])

    async def test_goal_text_requires_complete_fresh_exact_readback(self):
        conditions = [{"kind": "control_text", "target": IDENTITY, "expected": "new"}]
        request = AsyncMock(
            return_value={
                "status": "completed",
                "result": {"text_available": True, "text": "new", "text_truncated": False},
            }
        )
        self.assertTrue((await verify_conditions(conditions, request, "test"))["verified"])
        for data in (
            {},
            {"text_available": True, "text": "new", "text_truncated": True},
            {"text_available": True, "text": "new\n"},
        ):
            request.return_value = {"status": "completed", "result": data}
            self.assertFalse((await verify_conditions(conditions, request, "test"))["verified"])

    async def test_empty_keyboard_fallback_checks_every_chunk_and_final_readback(self):
        value = "longer than eight"
        contents = [""]

        def observe(*args):
            return {
                "text_available": True,
                "text": contents[0],
                "element": {
                    "states": {
                        "enabled": True,
                        "sensitive": True,
                        "showing": True,
                        "focused": True,
                        "editable": True,
                    }
                },
            }

        async def type_text(window, text, *, control_guard):
            for offset in range(0, len(text), 8):
                await control_guard(offset)
                contents[0] += text[offset : offset + 8]
            return {"input_sent": True, "characters": len(text)}

        native = SimpleNamespace(
            status=Mock(return_value={"connected": True}),
            type_text=AsyncMock(side_effect=type_text),
        )
        accessibility = SimpleNamespace(
            focus_empty_control=Mock(), inspect_control=Mock(side_effect=observe)
        )
        context = SimpleNamespace(
            accessibility=accessibility, desktop=SimpleNamespace(input=native)
        )
        with patch(
            "ev.tools.controls._active_accessibility_scope",
            AsyncMock(return_value=(123, "Fixture")),
        ):
            result = await control_type_empty({**IDENTITY, "text": value}, context)
        self.assertTrue(result["verified"])
        self.assertFalse(result["submitted"])
        self.assertEqual(contents[0], value)
        self.assertEqual(accessibility.inspect_control.call_count, 4)

    async def test_keyboard_fallback_stops_on_changed_text_and_never_connects_implicitly(self):
        native = SimpleNamespace(
            status=Mock(return_value={"connected": False}), type_text=AsyncMock()
        )
        accessibility = SimpleNamespace(
            focus_empty_control=Mock(),
            inspect_control=Mock(return_value={"text": "user changed this"}),
        )
        context = SimpleNamespace(
            accessibility=accessibility, desktop=SimpleNamespace(input=native)
        )
        with patch(
            "ev.tools.controls._active_accessibility_scope",
            AsyncMock(return_value=(123, "Fixture")),
        ):
            with self.assertRaisesRegex(RuntimeError, "not connected"):
                await control_type_empty({**IDENTITY, "text": "new"}, context)
            accessibility.focus_empty_control.assert_not_called()
            native.type_text.assert_not_awaited()
            native.status.return_value = {"connected": True}

            async def typed(window, text, *, control_guard):
                await control_guard(0)
                self.fail("Guard must prevent typing after a user edit")

            native.type_text.side_effect = typed
            with self.assertRaisesRegex(RuntimeError, "contents changed"):
                await control_type_empty({**IDENTITY, "text": "new"}, context)

    def test_native_empty_focus_refuses_existing_text_or_password_without_focusing(self):
        self.bridge.Atspi.Component = SimpleNamespace(grab_focus=Mock(return_value=True))
        for role, text in (("entry", "private existing"), ("password text", "")):
            self.bridge.inspect_control.return_value = {
                "text_available": True,
                "text": text,
                "element": {
                    "role": role,
                    "states": {"enabled": True, "sensitive": True, "showing": True},
                },
            }
            with self.assertRaises(RuntimeError):
                self.bridge.focus_empty_control("Fixture", "Search", role, 123, "Fixture", "/exact")
        self.bridge.Atspi.Component.grab_focus.assert_not_called()

    def test_empty_field_needs_positive_editable_state_not_just_an_interface(self):
        self.bridge.Atspi.Component = SimpleNamespace(grab_focus=Mock(return_value=True))
        states = {
            "enabled": True,
            "sensitive": True,
            "showing": True,
            "editable": True,
            "read_only": False,
        }
        self.bridge.inspect_control.return_value = {
            "text_available": True,
            "text": "",
            "element": {"role": "entry", "states": states},
        }
        for change in (
            {"editable": False},
            {"editable": None},
            {"editable": True, "read_only": True},
        ):
            states.update(change)
            with self.assertRaises(RuntimeError):
                self.bridge.focus_empty_control(
                    "Fixture", "Search", "entry", 123, "Fixture", "/exact"
                )
        self.bridge.Atspi.Component.grab_focus.assert_not_called()
        states.update(editable=True, read_only=False)
        result = self.bridge.focus_empty_control(
            "Fixture", "Search", "entry", 123, "Fixture", "/exact"
        )
        self.assertTrue(result["focus_requested"])
        self.assertFalse(result["verified"])
        self.bridge.Atspi.Component.grab_focus.assert_called_once_with(self.node)
