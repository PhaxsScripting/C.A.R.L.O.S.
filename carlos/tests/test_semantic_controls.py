import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from ev.accessibility import AccessibilityBridge
from ev.tools.controls import control_details, control_set
from ev.tools.controls import (
    controls_list,
    controls_activate,
    controls_resolve,
    CONTROL_IDENTITY_SCHEMA,
)
from ev.tools.base import validate_schema
from ev.tools.browser import browser_inspect


class SemanticBridgeTests(unittest.TestCase):
    def test_exact_native_file_chooser_is_a_window_scope_not_an_arbitrary_descendant(self):
        bridge = AccessibilityBridge.__new__(AccessibilityBridge)
        app, picker, other = Mock(), Mock(), Mock()
        app.get_name.return_value = "Fixture App"
        app.get_child_count.return_value = 2
        app.get_child_at_index.side_effect = [picker, other]
        picker.get_name.return_value = "Choose a file"
        picker.get_role_name.return_value = "file chooser"
        other.get_name.return_value = "Another window"
        other.get_role_name.return_value = "frame"
        self.assertEqual(bridge._window_roots(app, "Choose a file"), [picker])
        app.get_child_at_index.side_effect = [picker, picker]
        self.assertEqual(bridge._window_roots(app, "Choose a file"), [])
        app.get_child_at_index.side_effect = [picker, other]
        picker.get_role_name.return_value = "panel"
        self.assertEqual(bridge._window_roots(app, "Choose a file"), [])

    def setUp(self):
        self.bridge = AccessibilityBridge.__new__(AccessibilityBridge)
        self.node = Mock()
        self.before = {
            "path": "/exact",
            "name": "Option",
            "role": "check box",
            "states": {"enabled": True, "sensitive": True, "checked": False},
            "actions": ["toggle"],
        }
        self.bridge._resolve_node = Mock(return_value=(self.node, self.before))
        self.bridge.inspect_control = Mock(return_value={"element": {"states": {"checked": True}}})
        self.bridge.Atspi = SimpleNamespace(
            Value=SimpleNamespace(
                get_minimum_value=Mock(return_value=0),
                get_maximum_value=Mock(return_value=100),
                set_current_value=Mock(return_value=True),
            ),
            Selection=SimpleNamespace(select_child=Mock(return_value=True)),
        )

    def call(self, action="checked", value=True):
        return self.bridge.set_control(
            "App", "Option", self.before["role"], 123, "Window", "/exact", action, value
        )

    def test_checkbox_readback_not_action_acceptance(self):
        self.assertTrue(self.call()["verified"])
        self.node.do_action.assert_called_once_with(0)
        self.bridge.inspect_control.return_value = {"element": {"states": {"checked": False}}}
        self.assertFalse(self.call()["verified"])

    def test_native_link_jump_is_accepted_without_claiming_page_arrival(self):
        self.before.update(role="link", actions=["jump"])
        self.node.do_action.return_value = True
        result = self.bridge.activate("App", "Option", "link", 123, "Window")
        self.assertTrue(result["action_accepted"])
        self.assertEqual(result["action"], "jump")
        self.node.do_action.assert_called_once_with(0)
        self.assertNotIn("url", result)

    def test_unknown_actions_and_nonlink_jump_are_not_invoked(self):
        for role, action in (("button", "jump"), ("link", "delete"), ("link", "unknown")):
            self.before.update(role=role, actions=[action])
            with self.assertRaises(RuntimeError):
                self.bridge.activate("App", "Option", role, 123, "Window")
        self.node.do_action.assert_not_called()

    def test_already_checked_is_idempotent(self):
        self.before["states"]["checked"] = True
        self.assertTrue(self.call()["already_set"])
        self.node.do_action.assert_not_called()

    def test_unknown_disabled_or_wrong_path_refuses_action(self):
        for key in ("enabled", "sensitive"):
            with self.subTest(key=key):
                self.before["states"][key] = None
                with self.assertRaises(RuntimeError):
                    self.call()
                self.before["states"][key] = True
        self.before["path"] = "/other"
        with self.assertRaises(RuntimeError):
            self.call()
        self.node.do_action.assert_not_called()

    def test_slider_range_and_readback(self):
        self.before["role"] = "slider"
        self.bridge.inspect_control.return_value = {"element": {}, "value": 30.0}
        self.assertTrue(self.call("value", 30)["verified"])
        for value in (-1, 101, float("nan"), "30"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.call("value", value)
        self.bridge.Atspi.Value.set_current_value.assert_called_once()

    def test_tab_selection_checks_result(self):
        self.before["role"] = "page tab"
        self.bridge.inspect_control.return_value = {"element": {"states": {"selected": True}}}
        self.assertTrue(self.call("selected", True)["verified"])
        self.bridge.Atspi.Selection.select_child.assert_called_once()

    def test_table_cell_selection_reads_back_and_never_activates_the_row(self):
        self.before["role"] = "table cell"
        self.before["states"]["selected"] = False
        table = Mock()
        self.node.path = "/exact"
        self.bridge.Atspi.TableCell = SimpleNamespace(
            get_table=Mock(return_value=table), get_row_column_span=Mock(return_value=(2, 0, 1, 1))
        )
        self.bridge.Atspi.Table = SimpleNamespace(
            get_accessible_at=Mock(return_value=self.node),
            add_row_selection=Mock(return_value=True),
        )
        self.bridge.inspect_control.return_value = {"element": {"states": {"selected": True}}}
        self.assertTrue(self.call("selected", True)["verified"])
        self.bridge.Atspi.Table.add_row_selection.assert_called_once_with(table, 2)
        self.node.do_action.assert_not_called()
        self.bridge.inspect_control.return_value = {"element": {"states": {"selected": False}}}
        self.assertFalse(self.call("selected", True)["verified"])
        container = Mock(path="/container")
        container.get_child_count.return_value = 2
        container.get_child_at_index.side_effect = lambda i: [
            SimpleNamespace(path="/icon"),
            self.node,
        ][i]
        self.bridge.Atspi.Table.get_accessible_at.return_value = container
        self.bridge.inspect_control.return_value = {"element": {"states": {"selected": True}}}
        self.assertTrue(self.call("selected", True)["verified"])
        self.bridge.Atspi.Table.add_row_selection.reset_mock()
        self.bridge.Atspi.Table.get_accessible_at.return_value = SimpleNamespace(path="/different")
        with self.assertRaisesRegex(RuntimeError, "moved"):
            self.call("selected", True)
        self.bridge.Atspi.Table.add_row_selection.assert_not_called()

    def test_already_selected_row_is_not_reselected(self):
        self.before["role"] = "table cell"
        self.before["states"]["selected"] = True
        self.assertTrue(self.call("selected", True)["already_set"])
        self.bridge.Atspi.Selection.select_child.assert_not_called()

    def test_incomplete_unique_lookup_never_resolves_target(self):
        bridge = AccessibilityBridge.__new__(AccessibilityBridge)
        bridge.list_elements = Mock(return_value={"elements": [{"name": "One"}], "truncated": True})
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            bridge._resolve_node("App", "One", "button", 123, "Window")


class SemanticWrappersTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.args = {
            "window_id": "exact",
            "process_id": 123,
            "window_title": "Page",
            "application": "Firefox",
            "path": "/node",
            "name": "Option",
            "role": "check box",
        }
        self.window = {"id": "exact", "pid": 123, "title": "Page", "app_id": "firefox"}
        self.world = {"active_window_id": "exact", "windows": [self.window]}
        self.context = SimpleNamespace(
            desktop=SimpleNamespace(
                bridge=SimpleNamespace(request=AsyncMock(return_value=self.world)),
                snapshot=AsyncMock(return_value=self.world),
            ),
            accessibility=SimpleNamespace(
                inspect_control=Mock(return_value={"element": {}}),
                set_control=Mock(return_value={"verified": True}),
                list_elements=Mock(
                    return_value={
                        "elements": [
                            {
                                "path": "/node",
                                "name": "Option",
                                "role": "check box",
                                "states": {"checked": True},
                            }
                        ],
                        "truncated": False,
                    }
                ),
            ),
        )

    async def test_old_pid_refuses_read_and_write(self):
        self.args["process_id"] = 999
        with self.assertRaises(ValueError):
            await control_details(self.args, self.context)
        with self.assertRaises(ValueError):
            await control_set({**self.args, "state": "checked", "value": True}, self.context)
        self.context.accessibility.set_control.assert_not_called()

    async def test_focus_change_discards_verified_control_claim(self):
        with patch(
            "ev.tools.controls._active_accessibility_scope",
            AsyncMock(side_effect=[(123, "Page"), (456, "Other")]),
        ):
            result = await control_set(
                {**self.args, "state": "checked", "value": True}, self.context
            )
        self.assertFalse(result["verified"])

    async def test_browser_inspect_returns_bound_states_without_input(self):
        result = await browser_inspect({"window_id": "exact", "role": "check box"}, self.context)
        self.assertEqual(result["controls"][0]["process_id"], 123)
        self.assertTrue(result["complete"])
        self.assertFalse(result["page_url_verified"])

    async def test_partial_browser_tree_remains_partial(self):
        self.context.accessibility.list_elements.return_value["truncated"] = True
        result = await browser_inspect({"window_id": "exact"}, self.context)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertFalse(result["complete"])

    async def test_browser_change_discards_stale_controls(self):
        with patch(
            "ev.tools.browser.browser_scope",
            AsyncMock(side_effect=[(123, "Page"), (123, "Different tab")]),
        ):
            result = await browser_inspect({"window_id": "exact"}, self.context)
        self.assertFalse(result["ok"])
        self.assertNotIn("controls", result)

    async def test_numeric_controls_without_click_action_are_discoverable(self):
        self.context.accessibility.list_elements.return_value = {
            "elements": [
                {
                    "name": "Level",
                    "role": "slider",
                    "path": "/slider",
                    "application": "App",
                    "actions": [],
                    "value_control": True,
                }
            ]
        }
        result = await controls_list({"window_id": "exact"}, self.context)
        self.assertEqual(result["items"][0]["id"], "/slider")

    async def test_control_list_reports_its_own_display_limit_and_backend_errors(self):
        element = {
            "name": "Level",
            "role": "slider",
            "path": "/slider",
            "application": "App",
            "actions": [],
            "value_control": True,
        }
        self.context.accessibility.list_elements.return_value = {
            "elements": [element] * 31,
            "truncated": False,
        }
        result = await controls_list({"window_id": "exact"}, self.context)
        self.assertEqual(len(result["items"]), 30)
        self.assertTrue(result["truncated"])
        self.context.accessibility.list_elements.return_value = {"elements": [element], "errors": 1}
        self.assertTrue((await controls_list({"window_id": "exact"}, self.context))["truncated"])

    async def test_unselected_labels_do_not_crowd_out_actual_selectable_controls(self):
        label = {
            "name": "Paragraph",
            "role": "label",
            "path": "/label",
            "application": "App",
            "actions": [],
            "states": {"selected": False},
        }
        tab = {**label, "name": "Settings", "role": "page tab", "path": "/tab"}
        cell = {**label, "name": "fixture.txt", "role": "table cell", "path": "/file"}
        self.context.accessibility.list_elements.return_value = {
            "elements": [label] * 50 + [tab, cell]
        }
        result = await controls_list({"window_id": "exact"}, self.context)
        self.assertEqual([i["id"] for i in result["items"]], ["/tab", "/file"])
        self.assertFalse(result["truncated"])

    async def test_control_lookup_backend_error_refuses_click(self):
        self.context.accessibility.list_elements.return_value = {
            "elements": [{**self.args, "actions": ["click"]}],
            "errors": 1,
        }
        self.context.accessibility.activate = Mock()
        with self.assertRaises(ValueError):
            await controls_activate(self.args, self.context)
        self.context.accessibility.activate.assert_not_called()

    async def test_resolve_returns_only_exact_typed_identity_without_activation(self):
        element = {
            "name": "Level",
            "role": "slider",
            "path": "/slider",
            "application": "App",
            "actions": [],
            "value_control": True,
        }
        self.context.accessibility.list_elements.return_value = {"elements": [element]}
        self.context.accessibility.activate = Mock()
        result = await controls_resolve(
            {"window_id": "exact", "name": "Level", "role": "slider"}, self.context
        )
        validate_schema(result["target"], CONTROL_IDENTITY_SCHEMA)
        self.assertEqual(result["target"]["path"], "/slider")
        self.assertEqual(result["target"]["process_id"], 123)
        self.assertFalse(result["changed"])
        from ev.tools.results import evaluate_result

        self.assertTrue(evaluate_result("desktop.controls.resolve", result, read_only=True).ok)
        self.context.accessibility.activate.assert_not_called()

    async def test_resolve_rejects_partial_missing_duplicate_and_fuzzy_matches(self):
        element = {
            "name": "Level",
            "role": "slider",
            "path": "/slider",
            "application": "App",
            "actions": [],
            "value_control": True,
        }
        for listing in (
            {"elements": []},
            {"elements": [element, {**element, "path": "/second"}]},
            {"elements": [element], "truncated": True},
            {"elements": [element], "errors": 1},
            {"elements": [{**element, "name": "Level up"}]},
        ):
            self.context.accessibility.list_elements.return_value = listing
            with self.assertRaises(ValueError):
                await controls_resolve(
                    {"window_id": "exact", "name": "Level", "role": "slider"}, self.context
                )

    async def test_resolve_rejects_focus_change_during_observation(self):
        with patch(
            "ev.tools.controls._active_accessibility_scope",
            AsyncMock(side_effect=[(123, "Page"), (456, "Other")]),
        ):
            with self.assertRaises(ValueError):
                await controls_resolve(
                    {"window_id": "exact", "name": "Level", "role": "slider"}, self.context
                )
