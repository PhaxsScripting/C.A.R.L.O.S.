from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from ev.commands import direct_action, browser_input
from ev.daily import DailyStore, PowerController
from ev.events import PhaxEventBus
from ev.planner import TaskPlanner
from ev.state import StateMachine, CoreState
from ev.tools import ToolContext, ToolRegistry, register_builtin_tools
from ev.tools.daily import register_daily_tools, scene_catalog
from ev.spotify import SpotifyClient
from ev.security_center import SecurityCenter
from ev.tools.organization import register_organization_tools, preview, apply
from ev.service import CarlosCore
from ev.paths import Paths


class DailyUpgradeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DailyStore(Path(self.temp.name) / "daily.db")
        self.bus = PhaxEventBus()
        self.registry = ToolRegistry(
            ToolContext(
                {"security": {"max_tool_output_bytes": 100000, "allowed_roots": [self.temp.name]}},
                self.bus,
                logging.getLogger("daily-test"),
                daily=self.store,
            )
        )
        register_builtin_tools(self.registry)
        register_daily_tools(self.registry)
        register_organization_tools(self.registry)
        self.calls = []

        async def request(payload, correlation):
            self.calls.append(payload)
            tool = payload["name"]
            if tool == "desktop.window.resolve":
                return {
                    "status": "completed",
                    "result": {"resolved": True, "window": {"id": "window-exact-123"}},
                }
            return {
                "status": "completed",
                "result": {"verified": True, "message": "Native action verified."},
            }

        self.planner = TaskPlanner(self.registry, request, self.bus, StateMachine(self.bus))

    async def test_direct_window_requests_execute_without_model(self):
        for sentence, expected in (
            ("minimize my window", "minimize"),
            ("Could you please maximize Firefox?", "maximize"),
            ("restore my window", "restore"),
            ("I want you to fullscreen Firefox", "fullscreen"),
        ):
            with self.subTest(sentence=sentence):
                plan = self.planner.try_plan(sentence, "direct")
                self.assertIsNotNone(plan)
                self.assertEqual(plan.steps[-1].arguments["state"], expected)
                result = await self.planner.execute(plan)
                self.assertEqual(result["status"], "completed")
                self.assertEqual(self.calls[-1]["arguments"]["window_id"], "window-exact-123")

    async def test_power_is_intent_only_and_never_tested_on_host(self):
        for sentence, action in (
            ("shut down my computer", "shutdown"),
            ("turn my PC off", "shutdown"),
            ("restart my laptop", "reboot"),
            ("lock my screen", "lock"),
            ("put my computer to sleep", "suspend"),
            ("log out", "logout"),
        ):
            with self.subTest(sentence=sentence):
                parsed = direct_action(sentence)
                self.assertEqual(
                    (parsed.tool, parsed.arguments), ("system.power", {"action": action})
                )
                plan = self.planner.try_plan(sentence, "power")
                await self.planner.execute(plan)  # mocked requester only
        for sentence in (
            "How do I shut down my computer?",
            "don't shut down my PC",
            "explain how to minimize my window",
            "if I restart my computer",
            'type "shut down my computer"',
            "restart Firefox",
        ):
            with self.subTest(sentence=sentence):
                parsed = direct_action(sentence)
                self.assertTrue(parsed is None or parsed.tool != "system.power")

    async def test_power_countdown_cancels_before_any_native_command(self):
        power = PowerController(self.bus)
        with patch("ev.daily.asyncio.create_subprocess_exec", AsyncMock()) as spawn:
            result = await power.request("shutdown")
            self.assertTrue(result["scheduled"])
            self.assertTrue((await power.cancel())["cancelled"])
            spawn.assert_not_awaited()
        self.assertFalse(power.pending)

    async def test_reminder_timer_and_alarm_parsing(self):
        examples = {
            "set a five minute timer": 300,
            "set a timer for 20 seconds called tea": 20,
            "remind me to drink water in 2 hours": 7200,
        }
        for text, seconds in examples.items():
            action = direct_action(text)
            self.assertEqual(action.tool, "reminders.create")
            self.assertEqual(action.arguments["seconds"], seconds)
        self.assertEqual(
            direct_action("remind me to stretch every 30 minutes").arguments["repeat_seconds"], 1800
        )
        self.assertIsNotNone(direct_action("set an alarm for 8:30 pm tomorrow"))
        self.assertIsNone(direct_action("set an alarm for 25:99"))

    async def test_persistent_reminders_fire_once_and_recurring_skip_missed_ticks(self):
        single = self.store.add_reminder("tea", 1)["reminder"]
        repeat = self.store.add_reminder("stretch", 1, 60)["reminder"]
        reloaded = DailyStore(self.store.path)
        now = time.time() + 400
        self.assertEqual(len(reloaded.due(now)), 2)
        self.assertEqual(reloaded.due(now), [])
        self.assertGreater(reloaded.reminders()[0]["due"], now)
        self.assertTrue(reloaded.cancel_reminder(repeat["id"])["verified"])
        self.assertEqual(reloaded.reminders(), [])

    async def test_nicknames_are_explicit_durable_editable_and_removable(self):
        self.store.save("alias", "my editor", "code")
        self.assertEqual(DailyStore(self.store.path).records("alias"), {"my editor": "code"})
        self.store.save("alias", "my editor", "kate")
        self.assertEqual(self.store.records("alias")["my editor"], "kate")
        self.assertTrue(self.store.remove("alias", "my editor")["verified"])

    async def test_routine_composes_exact_window_references_and_rejects_power(self):
        self.planner.saved_routines = {
            "work": ["minimize Firefox", "maximize code"],
            "bad": ["shutdown"],
        }
        plan = self.planner.try_plan("run routine work", "routine")
        self.assertIsNotNone(plan)
        self.assertEqual(len(plan.steps), 4)
        self.assertEqual(
            plan.steps[-1].arguments["window_id"], {"$ref": "routine_1_window.result.window.id"}
        )
        self.assertIsNone(self.planner.try_plan("run routine bad", "blocked"))

    async def test_browser_navigation_has_no_shell_and_preserves_window(self):
        for text in (
            "open a new tab in Firefox",
            "reopen the last closed tab",
            "go back in Firefox",
            "refresh my page",
            "next tab",
        ):
            parsed = browser_input(text)
            self.assertIsNotNone(parsed, text)
            self.assertEqual(parsed["actions"][0]["tool"], "desktop.keyboard.key")
        self.assertIsNone(browser_input("go to javascript:alert(1)"))
        plan = self.planner.try_plan("go to https://example.com in Firefox", "navigation")
        self.assertIsNotNone(plan)
        self.assertEqual(
            next(step.arguments["text"] for step in plan.steps if step.tool.endswith("type_text")),
            "https://example.com",
        )

    async def test_firewall_reads_only_and_honestly_reports_missing_authorization(self):
        denied = {"ok": False, "stdout": "", "stderr": "Operation not permitted", "returncode": 1}
        center = SecurityCenter(Mock(), {})
        with patch("ev.security_center._run", return_value=denied) as run, patch(
            "ev.security_center.Path.is_file", return_value=True
        ), patch("ev.security_center.shutil.which", return_value="/usr/bin/sudo"):
            result = center.firewall_runtime()
            self.assertFalse(result["readable"])
            self.assertTrue(result["admin_authorization_required"])
            self.assertTrue(all("list" in call.args[0] for call in run.call_args_list))
            self.assertTrue(
                all("pkexec" not in " ".join(call.args[0]) for call in run.call_args_list)
            )

    async def test_spotify_ambiguous_selection_never_starts_playback(self):
        client = SpotifyClient(Path(self.temp.name) / "missing.json")
        client.search = AsyncMock(
            return_value={
                "items": [
                    {"name": "One", "artists": [], "uri": "spotify:track:" + "a" * 22},
                    {"name": "Two", "artists": [], "uri": "spotify:track:" + "b" * 22},
                ],
                "message": "Several matches.",
            }
        )
        client._api = AsyncMock()
        result = await client.play("something")
        self.assertFalse(result["verified"])
        client._api.assert_not_awaited()

    async def test_spotify_verified_playback_checks_selected_uri(self):
        client = SpotifyClient(Path(self.temp.name) / "missing.json")
        uri = "spotify:track:" + "a" * 22
        client._api = AsyncMock(side_effect=[{}, {"is_playing": True, "item": {"uri": uri}}])
        self.assertTrue((await client.play(uri))["verified"])
        self.assertEqual(client._api.await_args_list[0].args, ("PUT", "/me/player/play"))

    async def test_registry_has_unique_and_structurally_valid_new_tools(self):
        tools = self.registry.catalog()
        self.assertEqual(len(tools), len({tool["name"] for tool in tools}))
        for sentence in (
            "shutdown",
            "set a five minute timer",
            "show my reminders",
            "save alias editor as code",
            "run routine work",
            "go dark",
            "show Spotify playlists",
        ):
            action = direct_action(sentence)
            if action.tool == "routines.run":
                continue
            self.registry.validate(action.tool, action.arguments)

    async def test_organization_preview_apply_undo_without_overwrite(self):
        folder = Path(self.temp.name) / "downloads"
        folder.mkdir()
        original = folder / "notes.txt"
        original.write_text("important original")
        report = preview({"path": str(folder)}, self.registry.context)
        self.assertTrue(original.exists())
        args = {"preview_id": report["preview_id"]}
        self.assertEqual(apply(args, self.registry.context)["moved_files"], 1)
        self.assertFalse(original.exists())
        self.assertTrue(apply(args, self.registry.context, undo=True)["verified"])
        self.assertEqual(original.read_text(), "important original")
        report = preview({"path": str(folder)}, self.registry.context)
        (folder / "Documents" / "notes.txt").write_text("must not overwrite")
        with self.assertRaises(ValueError):
            apply({"preview_id": report["preview_id"]}, self.registry.context)
        self.assertEqual(original.read_text(), "important original")

    async def test_organization_rejects_changed_files_and_projects(self):
        folder = Path(self.temp.name)
        document = folder / "test.txt"
        document.write_text("before")
        report = preview({"path": str(folder)}, self.registry.context)
        document.write_text("after")
        with self.assertRaises(ValueError):
            apply({"preview_id": report["preview_id"]}, self.registry.context)
        (folder / "CMakeLists.txt").write_text("project(test)")
        with self.assertRaises(ValueError):
            preview({"path": str(folder)}, self.registry.context)
        self.assertEqual(document.read_text(), "after")

    async def test_other_window_correction_excludes_previous_exact_id(self):
        await self.planner.execute(self.planner.try_plan("minimize Firefox", "first"))
        correction = self.planner.try_plan("No, the other Firefox window", "second")
        self.assertEqual(correction.steps[0].arguments["exclude_window_id"], "window-exact-123")
        self.assertEqual(correction.steps[1].tool, "desktop.window.state")
        self.assertEqual(correction.steps[1].arguments["state"], "minimize")

    async def test_non_idempotent_actions_are_never_retried(self):
        for sentence in (
            "set a five minute timer",
            "go dark",
            "play music on spotify",
            "shutdown",
            "organize /tmp/example",
        ):
            plan = self.planner.try_plan(sentence, "retry")
            self.assertFalse(self.planner._retryable(plan.steps[-1]), sentence)

    async def test_power_cancel_does_not_claim_to_reverse_dispatched_request(self):
        power = PowerController(self.bus)
        power.pending = {"action": "shutdown", "phase": "dispatching"}
        result = await power.cancel()
        self.assertFalse(result["cancelled"])
        self.assertFalse(result["verified"])

    async def test_restore_all_windows_is_not_a_single_window_search(self):
        self.assertEqual(direct_action("restore all windows").tool, "desktop.show_desktop")

    async def test_wake_interrupts_thinking_without_cancelling_request_connection(self):
        folder = Path(self.temp.name)
        service = CarlosCore(
            paths=Paths(
                *(folder / name for name in ("config", "data", "state", "cache", "runtime"))
            )
        )
        self.addCleanup(service.memory.close)
        entered = asyncio.Event()

        async def thinking(*args):
            entered.set()
            await asyncio.Event().wait()

        service.brain.provider.begin = thinking
        request = asyncio.create_task(
            service._submit_action_clauses("tell me a story", "conversation")
        )
        await entered.wait()
        self.assertTrue(await service._interrupt_reasoning_for_wake())
        result = await request
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse(request.cancelled())
        self.assertIsNone(service._interactive_task)

    async def test_model_or_ipc_cannot_self_authorize_power_or_admin_dialog(self):
        folder = Path(self.temp.name)
        service = CarlosCore(
            paths=Paths(
                *(folder / name for name in ("config", "data", "state", "cache", "runtime"))
            )
        )
        self.addCleanup(service.memory.close)
        service.tools.execute = AsyncMock()
        for name, args in (
            ("system.power", {"action": "shutdown"}),
            ("security.firewall.runtime", {"authorize": True}),
        ):
            result = await service.request_tool(
                {"name": name, "arguments": args, "_trusted_plan": True}, "untrusted"
            )
            self.assertEqual(result["status"], "failed")
        service.tools.execute.assert_not_awaited()

    async def test_firewall_authorized_read_has_fixed_argv_and_no_rule_changes(self):
        denied = {"ok": False, "stdout": "", "stderr": "Operation not permitted", "returncode": 1}
        allowed = {
            "ok": True,
            "stdout": json.dumps(
                {"nftables": [{"chain": {"name": "input", "policy": "drop"}}, {"rule": {}}]}
            ),
            "stderr": "",
            "returncode": 0,
        }
        with patch("ev.security_center._run", side_effect=[denied, denied, allowed]) as run, patch(
            "ev.security_center.Path.is_file", return_value=True
        ), patch("ev.security_center.shutil.which", return_value="available"):
            result = SecurityCenter(Mock(), {}).firewall_runtime(authorize=True)
            self.assertEqual(
                run.call_args.args[0], ["/usr/bin/pkexec", "/usr/bin/nft", "-j", "list", "ruleset"]
            )
            self.assertEqual(result["rule_count"], 1)
            self.assertFalse(result["rules_changed"])


if __name__ == "__main__":
    unittest.main()
