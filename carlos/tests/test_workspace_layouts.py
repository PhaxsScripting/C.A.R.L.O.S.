import tempfile
import time
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from ev.daily import DailyStore
from ev.goals import verify_conditions
from ev.tools.workspaces import capture, restore_plan, resolve_saved
from ev.tools.base import ValidationError


class WorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DailyStore(Path(self.temp.name) / "daily.db")
        self.window = {
            "id": "exact",
            "pid": 123,
            "app_id": "editor",
            "resource_class": "Editor",
            "title": "Private filename",
            "geometry": {"x": 20, "y": 30, "width": 640, "height": 480},
            "normal": True,
            "minimized": False,
            "maximized": False,
            "fullscreen": False,
            "desktops": ["main"],
            "output": "monitor",
        }
        self.world = {
            "windows": [self.window],
            "active_window_id": "exact",
            "outputs": [
                {"name": "monitor", "geometry": {"x": 0, "y": 0, "width": 1920, "height": 1080}}
            ],
        }
        self.context = SimpleNamespace(
            daily=self.store,
            desktop=SimpleNamespace(
                snapshot=AsyncMock(side_effect=lambda **kw: deepcopy(self.world))
            ),
        )
        self.process = patch("ev.tools.workspaces._process_start", return_value=123.0)
        self.process.start()
        self.addCleanup(self.process.stop)
        self.boot = patch("ev.tools.workspaces._boot_id", return_value="boot")
        self.boot.start()
        self.addCleanup(self.boot.stop)

    async def save(self):
        return await capture({"name": "Coding", "window_ids": ["exact"]}, self.context)

    async def test_capture_persists_selected_metadata_not_private_title(self):
        self.assertTrue((await self.save())["verified"])
        reopened = DailyStore(self.store.path)
        records = reopened.records("workspace_layout")
        self.assertEqual(records["coding"]["windows"][0]["process_start"], 123.0)
        self.assertNotIn("Private filename", str(records))
        self.assertEqual(self.window["geometry"]["x"], 20)

    async def test_duplicate_name_preserves_original(self):
        await self.save()
        self.window["geometry"]["x"] = 200
        with self.assertRaises(ValidationError):
            await self.save()
        self.assertEqual(
            self.store.records("workspace_layout")["coding"]["windows"][0]["geometry"]["x"], 20
        )

    async def test_restore_plan_revalidates_then_restores_geometry(self):
        await self.save()
        self.window["geometry"]["x"] = 200
        result = await restore_plan({"name": "coding"}, self.context)
        self.assertEqual(
            [s["tool"] for s in result["plan"]["steps"]],
            ["workspaces.resolve_window", "desktop.window.state", "desktop.window.move_resize"],
        )
        self.assertEqual(result["plan"]["steps"][2]["arguments"]["x"], 20)
        self.assertFalse(result["changed"])
        self.assertFalse(result["exact_session_restoration"])

    async def test_fullscreen_or_minimized_current_window_is_restored_before_geometry(self):
        await self.save()
        self.window.update(fullscreen=True, minimized=True)
        result = await restore_plan({"name": "coding"}, self.context)
        self.assertEqual(result["plan"]["steps"][1]["arguments"]["state"], "restore")
        self.assertEqual(result["plan"]["conditions"][-1]["expected"], False)

    async def test_missing_window_not_substituted_by_same_app(self):
        await self.save()
        self.window["id"] = "new-same-app"
        result = await restore_plan({"name": "coding"}, self.context)
        self.assertIsNone(result["plan"])
        self.assertEqual(len(result["gaps"]), 1)

    async def test_reused_pid_or_reboot_refuses_restoration(self):
        await self.save()
        with patch("ev.tools.workspaces._process_start", return_value=456.0), self.assertRaises(
            ValidationError
        ):
            await resolve_saved({"name": "coding", "window_id": "exact"}, self.context)
        with patch("ev.tools.workspaces._boot_id", return_value="new-boot"), self.assertRaises(
            ValidationError
        ):
            await resolve_saved({"name": "coding", "window_id": "exact"}, self.context)

    async def test_monitor_topology_change_requires_replanning(self):
        await self.save()
        self.world["outputs"][0]["geometry"]["width"] = 1000
        result = await restore_plan({"name": "coding"}, self.context)
        self.assertIsNone(result["plan"])
        self.assertFalse(result["all_saved_windows_resolvable"])

    async def test_maximized_and_minimized_states_generate_postconditions(self):
        self.window.update(maximized=True, minimized=True)
        await self.save()
        result = await restore_plan({"name": "coding"}, self.context)
        steps = result["plan"]["steps"]
        self.assertEqual(
            [s["arguments"].get("state") for s in steps[-2:]], ["maximize", "minimize"]
        )
        self.assertEqual(
            [c["property"] for c in result["plan"]["conditions"]], ["maximized", "minimized"]
        )

    async def test_special_window_and_duplicate_id_not_saved(self):
        with self.assertRaises(ValidationError):
            await capture({"name": "bad", "window_ids": ["exact", "exact"]}, self.context)
        self.window["special"] = True
        with self.assertRaises(ValidationError):
            await self.save()
        self.assertEqual(self.store.records("workspace_layout"), {})

    async def test_geometry_predicate_uses_real_fresh_bounds(self):
        condition = [
            {
                "kind": "window_geometry",
                "window_id": "exact",
                "geometry": deepcopy(self.window["geometry"]),
            }
        ]
        self.world["captured_at_monotonic"] = time.monotonic()
        request = AsyncMock(return_value={"status": "completed", "result": self.world})
        self.assertTrue((await verify_conditions(condition, request, "test"))["verified"])
        self.window["geometry"]["width"] = 600
        self.assertFalse((await verify_conditions(condition, request, "test"))["verified"])

    async def test_context_is_explicit_path_metadata_without_file_contents(self):
        root = Path(self.temp.name)
        document = root / "work.cpp"
        document.write_text("PRIVATE_BUFFER_CANARY")
        self.context.config = {"security": {"allowed_roots": [str(root)]}}
        await capture(
            {
                "name": "context",
                "window_ids": ["exact"],
                "context": {
                    "project": str(root),
                    "editor_files": [str(document)],
                    "terminal_cwds": [str(root)],
                    "codex_session": "session-reference",
                },
            },
            self.context,
        )
        stored = self.store.records("workspace_layout")["context"]
        self.assertNotIn("PRIVATE_BUFFER_CANARY", str(stored))
        document.unlink()
        planned = await restore_plan({"name": "context"}, self.context)
        editor = next(c for c in planned["context_checks"] if c["kind"] == "editor_files")
        self.assertFalse(editor["available"])
        self.assertFalse(editor["restored"])

    async def test_context_rejects_paths_outside_allowed_roots(self):
        self.context.config = {"security": {"allowed_roots": [self.temp.name]}}
        with self.assertRaises(ValidationError):
            await capture(
                {"name": "bad", "window_ids": ["exact"], "context": {"project": "/etc"}},
                self.context,
            )
        self.assertEqual(self.store.records("workspace_layout"), {})

    async def test_existing_virtual_desktop_assignment_is_restored(self):
        self.world["desktops"] = [{"id": "main", "name": "Main"}]
        await self.save()
        result = await restore_plan({"name": "coding"}, self.context)
        step = next(
            s for s in result["plan"]["steps"] if s["tool"] == "desktop.window.move_to_workspace"
        )
        self.assertEqual(step["arguments"]["desktop_id"], "main")

    async def test_current_capture_rejects_large_desktop_without_partial_save(self):
        from ev.tools.workspaces import capture_current

        self.world["windows"] = [{**self.window, "id": str(i)} for i in range(6)]
        with self.assertRaises(ValidationError):
            await capture_current({"name": "large"}, self.context)
        self.assertEqual(self.store.records("workspace_layout"), {})

    async def test_missing_app_gets_fresh_relaunch_plan_not_stale_window_id(self):
        with patch(
            "ev.tools.workspaces.desktop_entries",
            return_value={"editor": {"_startup_wm_class": "Editor"}},
        ):
            await self.save()
        self.world["windows"] = []
        result = await restore_plan({"name": "coding"}, self.context)
        self.assertEqual(result["plan"]["steps"][0]["tool"], "workspaces.relaunch_window")
        self.assertEqual(
            result["plan"]["conditions"][0]["window_id"], {"$ref": "window0.result.window.id"}
        )

    async def test_relaunch_requires_unique_fresh_window_and_matching_catalog(self):
        from ev.tools.workspaces import relaunch_saved

        catalog = {"editor": {"_startup_wm_class": "Editor"}}
        with patch("ev.tools.workspaces.desktop_entries", return_value=catalog):
            await self.save()
            self.world["windows"] = []

            def launch(*args):
                self.world["windows"] = [{**self.window, "id": "fresh", "pid": 456}]
                return {"launched": True}

            with patch("ev.tools.workspaces.open_application", side_effect=launch) as opened:
                result = await relaunch_saved(
                    {"name": "coding", "window_id": "exact"}, self.context
                )
                self.assertEqual(result["window"]["id"], "fresh")
                opened.assert_called_once()
                with self.assertRaises(ValidationError):
                    await relaunch_saved({"name": "coding", "window_id": "exact"}, self.context)
                self.assertEqual(opened.call_count, 1)

    async def test_changed_catalog_and_topology_never_launch(self):
        from ev.tools.workspaces import relaunch_saved

        with patch(
            "ev.tools.workspaces.desktop_entries",
            return_value={"editor": {"_startup_wm_class": "Editor"}},
        ):
            await self.save()
        self.world["windows"] = []
        with patch("ev.tools.workspaces.desktop_entries", return_value={}), patch(
            "ev.tools.workspaces.open_application"
        ) as opened:
            with self.assertRaises(ValidationError):
                await relaunch_saved({"name": "coding", "window_id": "exact"}, self.context)
            opened.assert_not_called()


class WorkspacePreviewRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_routes_through_executor_dry_run_without_relaunching(self):
        from ev.service import CarlosCore

        for text in (
            "Dry run restore BNM",
            "Preview restore workspace BNM",
            "Just show me restore layout BNM",
        ):
            with self.subTest(text=text):
                service = CarlosCore.__new__(CarlosCore)
                service._action_generation = 0
                service.daily = SimpleNamespace(
                    records=lambda kind: {"bnm": {}} if kind == "workspace_layout" else {}
                )
                service.planner = SimpleNamespace(
                    execute=AsyncMock(return_value={"status": "completed"})
                )
                service._request_model_tool = AsyncMock(
                    return_value={
                        "status": "completed",
                        "result": {"plan": {"goal": "Restore"}, "gaps": []},
                    }
                )
                plan = SimpleNamespace(dry_run=False)
                with patch("ev.tools.plans.build_plan", return_value=plan):
                    await service._submit_action_clauses_impl(text, "preview")
                self.assertTrue(plan.dry_run)
                self.assertEqual(
                    service._request_model_tool.await_args.args[0]["name"],
                    "workspaces.restore_plan",
                )
                service.planner.execute.assert_awaited_once_with(plan)
