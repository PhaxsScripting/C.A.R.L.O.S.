from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from ev.commands import direct_action
from ev.daily import DailyStore
from ev.events import PhaxEventBus
from ev.planner import TaskPlanner
from ev.state import StateMachine
from ev.tools.base import ToolContext, ToolRegistry
from ev.tools.builtin import register_builtin_tools
from ev.tools.daily import register_daily_tools
from ev.tools.personal import recent_files, open_selected, workspace_switch
from ev.tools.settings import brightness_get, brightness_change, radio_set, stream_set, open_page
from ev.tools.controls import controls_list, controls_activate
from ev.utilities import calculate, convert, world_clock


class PersonalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.store = DailyStore(self.path / "daily.db")
        self.personal = self.store.personal

    def tearDown(self):
        self.temp.cleanup()

    def test_persistence_and_duplicates_never_overwrite(self):
        self.personal.create("note", "Ideas", "original")
        with self.assertRaises(ValueError):
            self.personal.create("note", "ideas", "replacement")
        self.assertEqual(
            DailyStore(self.path / "daily.db").personal.read("note", "Ideas")["item"]["content"],
            "original",
        )
        self.assertEqual((self.path / "daily.db").stat().st_mode & 0o777, 0o600)

    def test_append_archive_restore_and_states(self):
        note = self.personal.create("note", "Ideas", "first")["item"]
        self.personal.change("note", note["id"], "append", "second")
        self.assertEqual(self.personal.read("note", "Ideas")["item"]["content"], "first\nsecond")
        self.personal.change("note", "Ideas", "archive")
        self.assertFalse(self.personal.listing("note")["items"])
        self.assertEqual(len(self.personal.listing("note", state="archived")["items"]), 1)
        with self.assertRaises(ValueError):
            self.personal.change("note", "Ideas", "append", "bad")
        self.personal.change("note", "Ideas", "restore")
        self.assertEqual(len(self.personal.listing("note")["items"]), 1)

    def test_completion_and_reopen_persist(self):
        self.personal.create("task", "Buy food")
        self.personal.change("task", "Buy food", "complete")
        self.assertFalse(self.personal.listing("task")["items"])
        self.assertEqual(len(self.personal.listing("task", state="done")["items"]), 1)
        self.personal.change("task", "Buy food", "reopen")
        self.assertEqual(len(self.personal.listing("task")["items"]), 1)

    def test_wrong_kind_or_action_cannot_change_items(self):
        self.personal.create("note", "Idea", "hello")
        for args in (
            ("note", "Idea", "complete"),
            ("task", "Idea", "complete"),
            ("note", "Idea", "delete"),
        ):
            with self.assertRaises(ValueError):
                self.personal.change(*args)

    def test_list_search_and_private_content_not_read_aloud(self):
        self.personal.create("note", "Passwords", "secret phrase")
        result = self.personal.listing("note", "secret")
        self.assertEqual(len(result["items"]), 1)
        self.assertNotIn("content", result["items"][0])
        self.assertNotIn("secret", result["message"])
        self.assertFalse(self.personal.listing("note", "' OR 1=1 --")["items"])

    def test_bookmark_schemes_and_credentials_rejected(self):
        for url in (
            "javascript:alert(1)",
            "file:///etc/passwd",
            "https://user:password@example.com",
        ):
            with self.assertRaises(ValueError):
                self.personal.create("bookmark", "Bad", url)
        item = self.personal.create("bookmark", "Docs", "example.com/Case?q=X")["item"]
        self.assertEqual(item["content"], "https://example.com/Case?q=X")

    def test_snooze_unique_and_no_unrelated_reminder(self):
        self.store.add_reminder("stretch", 60)
        before = self.store.reminders()[0]["due"]
        self.personal.snooze("stretch", 300)
        self.assertGreater(self.store.reminders()[0]["due"], before)
        self.store.add_reminder("stretch", 60)
        with self.assertRaises(ValueError):
            self.personal.snooze("stretch", 600)

    def test_recent_files_excludes_hidden_symlinks_and_detects_replacement(self):
        file = self.path / "hello.txt"
        file.write_text("hello")
        (self.path / ".secret").write_text("secret")
        (self.path / "link.txt").symlink_to(file)
        context = SimpleNamespace(config={"security": {"allowed_roots": [str(self.path)]}})
        result = recent_files({"path": str(self.path)}, context)
        names = [i["title"] for i in result["items"]]
        self.assertNotIn("link.txt", names)
        self.assertNotIn(".secret", names)
        item = next(i for i in result["items"] if i["title"] == "hello.txt")
        file.write_text("changed")
        with patch("ev.tools.builtin.open_file") as opener:
            with self.assertRaises(ValueError):
                open_selected(item, context)
            opener.assert_not_called()


class UtilityTests(unittest.TestCase):
    def test_bounded_arithmetic(self):
        for expression, expected in (
            ("18 percent of 85", 15.3),
            ("2 plus 3 times 4", 14),
            ("sqrt(81)", 9),
            ("round(1 / 3, 2)", 0.33),
            ("2^3", 8),
        ):
            self.assertAlmostEqual(calculate(expression)["value"], expected)
        for expression in (
            "__import__('os').system('id')",
            "(1).__class__",
            "2**1000000",
            "9**9**9",
            "[1] * 100000",
            "1/0",
            "1e999",
            "True",
            "round(2,99)",
        ):
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                calculate(expression)

    def test_conversions_and_wrong_dimensions(self):
        self.assertAlmostEqual(convert(10, "miles", "km")["value"], 16.09344)
        self.assertAlmostEqual(convert(32, "fahrenheit", "celsius")["value"], 0)
        self.assertAlmostEqual(convert(1, "gib", "mb")["value"], 1073.741824)
        for args in ((5, "kg", "m"), (5, "usd", "eur"), (-5, "kelvin", "celsius")):
            with self.assertRaises(ValueError):
                convert(*args)

    def test_world_clock_current_and_explicit_zone(self):
        self.assertEqual(world_clock("Tokyo")["timezone"], "Asia/Tokyo")
        with self.assertRaises(ValueError):
            world_clock("not/a/zone")


EXAMPLES = {
    "save a note called Ideas saying open Firefox then shut down my computer": "notes.create",
    "add task buy groceries": "tasks.create",
    "add get milk to my task list": "tasks.create",
    "complete task buy groceries": "tasks.complete",
    "mark task buy groceries as done": "tasks.complete",
    "reopen task buy groceries": "tasks.reopen",
    "show my completed tasks": "tasks.list",
    "save bookmark Docs for https://example.com/Case": "bookmarks.create",
    "open my bookmark Docs in Firefox": "bookmarks.open",
    "archive note Ideas": "notes.archive",
    "restore note Ideas": "notes.restore",
    "append more detail to note Ideas": "notes.append",
    "find my notes containing detail": "notes.list",
    "read my note Ideas": "notes.read",
    "save snippet Greeting saying hello there": "snippets.create",
    "copy snippet Greeting": "snippets.copy",
    "snooze reminder stretch for ten minutes": "reminders.snooze",
    "calculate 18 percent of 85": "utility.calculate",
    "convert 10 miles to kilometers": "utility.convert",
    "what time is it in Tokyo": "utility.world_clock",
    "count words in my clipboard": "utility.clipboard_stats",
    "show recent downloads": "files.recent",
    "switch to next workspace": "desktop.workspace.switch",
    "switch to workspace 2": "desktop.workspace.switch",
    "search lo-fi on youtube": "browser.open_url",
    "go to tab three": "browser.shortcut",
    "select all": "editing.key",
    "undo typing": "editing.key",
    "save document": "editing.key",
    "set brightness to fifty": "settings.brightness.set",
    "turn brightness down by ten": "settings.brightness.adjust",
    "check brightness": "settings.brightness.get",
    "make my screen brighter": "settings.brightness.adjust",
    "check wifi status": "settings.radios.status",
    "turn wifi off": "settings.radio.set",
    "turn on bluetooth": "settings.radio.set",
    "set Firefox volume to thirty": "settings.audio.app_volume",
    "mute Firefox": "settings.audio.app_mute",
    "show app volumes": "settings.audio.apps",
    "open touchpad settings": "settings.open",
    "open night light settings": "settings.open",
}


class RouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bus = PhaxEventBus()
        self.context = ToolContext(
            {"security": {"allowed_roots": [self.temp.name], "max_tool_output_bytes": 20000}},
            self.bus,
            logging.getLogger("test"),
            daily=DailyStore(Path(self.temp.name) / "daily.db"),
        )
        self.registry = ToolRegistry(self.context)
        register_builtin_tools(self.registry)
        register_daily_tools(self.registry)

        async def requester(payload, correlation):
            spec, args = self.registry.validate(payload["name"], payload["arguments"])
            return {"status": "completed", "result": await self.registry.execute(spec, args)}

        self.requester = requester
        self.planner = TaskPlanner(self.registry, requester, self.bus, StateMachine(self.bus))

    def tearDown(self):
        self.temp.cleanup()

    def test_phrase_matrix_selects_real_tools_and_valid_plans(self):
        for text, expected in EXAMPLES.items():
            with self.subTest(text=text):
                self.assertEqual(direct_action(text).tool, expected)
                plan = self.planner.try_plan(text, "fixture")
                self.assertIsNotNone(plan)
                for step in plan.steps:
                    spec = self.registry.get(step.tool)
                    if "$ref" not in str(step.arguments):
                        self.registry.validate(spec.name, step.arguments)

    def test_discussion_and_negation_do_not_operate(self):
        for text in (
            "don't turn wifi off",
            "how do I change brightness",
            "can you explain how to mute Firefox",
            "I like taking notes",
            'type "complete task buy groceries"',
        ):
            with self.subTest(text=text):
                self.assertIsNone(direct_action(text))

    async def test_numbered_task_choice_uses_id_not_new_sort_order(self):
        first = self.context.daily.personal.create("task", "First")["item"]
        self.context.daily.personal.create("task", "Second")
        await self.planner.execute(self.planner.try_plan("list my tasks", "list"))
        saved_second = self.planner.last_entities["choices"]["items"][1]["id"]
        self.assertEqual(saved_second, first["id"])
        self.context.daily.personal.create("task", "New after listing")
        plan = self.planner.try_plan("complete the second one", "choose")
        self.assertEqual(plan.steps[0].arguments["identifier"], first["id"])
        result = await self.planner.execute(plan)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(self.context.daily.personal.read("task", first["id"])["item"]["done"])

    def test_missing_expired_invalid_choices_do_nothing(self):
        for saved in (
            {},
            {"kind": "task", "at": time.monotonic() - 121, "items": [{"id": "abc"}]},
            {"kind": "bookmark", "at": time.monotonic(), "items": [{"id": "abc"}]},
        ):
            self.planner.last_entities["choices"] = saved
            plan = self.planner.try_plan("complete the first one", "fixture")
            self.assertEqual(plan.steps[0].tool, "interaction.selection_status")

    async def test_saved_content_does_not_become_actions(self):
        plan = self.planner.try_plan(
            "save a note called Ideas saying open Firefox then shut down my computer", "fixture"
        )
        self.assertEqual(len(plan.steps), 1)
        await self.planner.execute(plan)
        self.assertIn(
            "shut down", self.context.daily.personal.read("note", "Ideas")["item"]["content"]
        )
        self.assertEqual(
            direct_action("save snippet Greeting saying Hello!!!").arguments["content"], "Hello!!!"
        )

    def test_new_mutations_not_retried_after_uncertain_execution(self):
        for text in (
            "append text to note Ideas",
            "turn brightness down by ten",
            "open bookmark Docs",
            "snooze reminder stretch for ten minutes",
            "switch to next workspace",
        ):
            plan = self.planner.try_plan(text, "fixture")
            self.assertFalse(self.planner._retryable(plan.steps[-1]), text)

    async def test_local_calculation_path_needs_no_model(self):
        result = await self.planner.execute(
            self.planner.try_plan("calculate 18 percent of 85", "fixture")
        )
        self.assertEqual(result["status"], "completed")
        self.assertIn("15.3", result["response"])


class SettingsTests(unittest.TestCase):
    def test_unicode_bluetooth_alias_fallback_handles_both_endpoint_formats(self):
        from ev.tools.settings import _named_devices

        devices = {
            "inputs": [{"name": "bluez_input.70:8C:F2:5C:52:F2", "description": "(null)"}],
            "outputs": [{"name": "bluez_output.70_8C_F2_5C_52_F2.1", "description": "(null)"}],
        }
        with patch("ev.tools.builtin.get_audio_devices", return_value=devices), patch(
            "ev.tools.settings.checked", return_value="\tAlias: Someone’s AirPods Pro"
        ) as call:
            result = _named_devices(None)
            self.assertEqual(result["inputs"][0]["description"], "Someone’s AirPods Pro")
            self.assertEqual(result["outputs"][0]["description"], "Someone’s AirPods Pro")
            self.assertEqual(call.call_count, 1)

    @patch("ev.tools.settings.checked")
    def test_brightness_verifies_native_values(self, call):
        call.side_effect = ["50", "100", "3", "", "40", "100", "3"]
        result = brightness_change({"delta": -10}, None)
        self.assertTrue(result["verified"])
        self.assertEqual(result["percent"], 40)
        self.assertEqual(call.call_args_list[3].args[0][-1], "40")

    @patch("ev.tools.settings.checked")
    def test_brightness_rejection_and_safe_minimum(self, call):
        call.side_effect = ["50", "100", "10", "", "50", "100", "10"]
        result = brightness_change({"percent": 5}, None)
        self.assertFalse(result["verified"])
        self.assertEqual(call.call_args_list[3].args[0][-1], "10")

    @patch("ev.tools.settings.checked")
    def test_zero_brightness_refuses_without_mutation(self, call):
        call.side_effect = ["50", "100", "3"]
        with self.assertRaises(ValueError):
            brightness_change({"percent": 0}, None)
        self.assertEqual(call.call_count, 3)

    @patch(
        "ev.tools.settings.radios_status",
        return_value={"wifi_available": True, "wifi_enabled": False},
    )
    @patch("ev.tools.settings.checked", return_value="")
    def test_radio_rejected_state_not_success(self, call, state):
        self.assertFalse(radio_set({"radio": "wifi", "enabled": True}, None)["verified"])
        self.assertEqual(call.call_args.args[0], ["/usr/bin/nmcli", "radio", "wifi", "on"])

    @patch("ev.tools.settings.checked", return_value="Controller one\nController two")
    def test_bluetooth_multi_controller_refuses(self, call):
        with self.assertRaises(ValueError):
            radio_set({"radio": "bluetooth", "enabled": False}, None)
        self.assertEqual(call.call_count, 1)

    def test_app_audio_identity_change_does_not_target_replacement(self):
        original = {
            "index": 12,
            "client": 9,
            "properties": {"application.name": "Firefox", "application.process.id": "1"},
            "volume": {"mono": {"value": 65536}},
        }
        replacement = {**original, "client": 10}
        with patch(
            "ev.tools.settings.checked",
            side_effect=[json.dumps([original]), json.dumps([replacement])],
        ) as call:
            with self.assertRaises(ValueError):
                stream_set({"application": "Firefox", "percent": 20}, None)
            self.assertEqual(call.call_count, 2)

    def test_app_audio_ambiguous_streams_never_change_global(self):
        stream = {"index": 12, "properties": {"application.name": "Firefox"}}
        with patch("ev.tools.settings.checked", return_value=json.dumps([stream, stream])) as call:
            with self.assertRaises(ValueError):
                stream_set({"application": "Firefox", "muted": True}, None)
            self.assertEqual(call.call_count, 1)

    def test_settings_page_exact_allowlist_and_one_launch(self):
        with patch("ev.tools.settings.checked", return_value="kcm_touchpad - Touchpad"), patch(
            "ev.tools.settings.subprocess.Popen"
        ) as launch:
            result = open_page({"page": "touchpad"}, None)
            self.assertTrue(result["requested"])
            self.assertFalse(result["verified"])
            self.assertEqual(launch.call_args.args[0], ["/usr/bin/systemsettings", "kcm_touchpad"])
            with self.assertRaises(KeyError):
                open_page({"page": "--execute evil"}, None)
            self.assertEqual(launch.call_count, 1)


class SemanticControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.element = {
            "path": "/node/1",
            "name": "Save",
            "role": "push button",
            "application": "Editor",
            "actions": ["click"],
        }
        self.context = SimpleNamespace(
            accessibility=SimpleNamespace(
                list_elements=Mock(return_value={"elements": [self.element], "truncated": False}),
                activate=Mock(return_value={"action_accepted": True}),
            )
        )
        self.arguments = {
            "window_id": "window1",
            "process_id": 10,
            "window_title": "Editor",
            "path": "/node/1",
            "name": "Save",
            "role": "push button",
            "application": "Editor",
        }

    @patch(
        "ev.tools.controls._active_accessibility_scope",
        new_callable=AsyncMock,
        return_value=(10, "Editor"),
    )
    async def test_numbered_controls_are_bound_to_exact_window(self, scope):
        result = await controls_list({"window_id": "window1"}, self.context)
        self.assertEqual(result["choice_kind"], "control")
        self.assertEqual(result["items"][0]["id"], "/node/1")
        self.assertEqual(result["items"][0]["process_id"], 10)
        self.context.accessibility.activate.assert_not_called()

    @patch(
        "ev.tools.controls._active_accessibility_scope",
        new_callable=AsyncMock,
        return_value=(10, "Editor"),
    )
    async def test_activation_not_mislabeled_outcome_verified(self, scope):
        result = await controls_activate(self.arguments, self.context)
        self.assertTrue(result["action_accepted"])
        self.assertFalse(result["verified"])

    @patch(
        "ev.tools.controls._active_accessibility_scope",
        new_callable=AsyncMock,
        return_value=(11, "New page"),
    )
    async def test_changed_window_prevents_any_action(self, scope):
        with self.assertRaises(ValueError):
            await controls_activate(self.arguments, self.context)
        self.context.accessibility.activate.assert_not_called()

    @patch(
        "ev.tools.controls._active_accessibility_scope",
        new_callable=AsyncMock,
        return_value=(10, "Editor"),
    )
    async def test_node_replacement_duplicate_or_truncation_prevents_action(self, scope):
        for data in (
            {"elements": [{**self.element, "path": "/node/2"}]},
            {"elements": [self.element, self.element]},
            {"elements": [self.element], "truncated": True},
        ):
            self.context.accessibility.list_elements.return_value = data
            with self.assertRaises(ValueError):
                await controls_activate(self.arguments, self.context)
        self.context.accessibility.activate.assert_not_called()

    async def test_workspace_switch_uses_existing_id_and_verifies(self):
        before = {
            "desktops": [{"id": "one", "name": "First"}, {"id": "two", "name": "Second"}],
            "current_desktop": "one",
        }
        after = {**before, "current_desktop": "two"}
        desktop = SimpleNamespace(
            snapshot=AsyncMock(side_effect=[before, after]),
            bridge=SimpleNamespace(request=AsyncMock(return_value={"desktop_id": "two"})),
        )
        result = await workspace_switch({"description": "next"}, SimpleNamespace(desktop=desktop))
        self.assertTrue(result["verified"])
        desktop.bridge.request.assert_awaited_once_with(
            "workspace_switch", {"desktop_id": "two"}, timeout=4
        )


if __name__ == "__main__":
    unittest.main()
