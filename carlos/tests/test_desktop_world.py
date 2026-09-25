from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "core"))

from ev.desktop import DesktopWorldModel, KWinBridge  # noqa: E402
from ev.desktop.world import EntityResolutionError  # noqa: E402
from ev.tools.builtin import desktop_activate_window  # noqa: E402


class DesktopWorldTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = DesktopWorldModel(KWinBridge(Path("/missing"), logging.getLogger("test")))
        self.world = {
            "active_window_id": "firefox-1",
            "active_output": "HDMI-A-1",
            "current_desktop": "desktop-1",
            "desktops": [
                {"id": "desktop-1", "name": "Main"},
                {"id": "desktop-2", "name": "Work"},
            ],
            "outputs": [
                {
                    "id": 1,
                    "name": "eDP-1",
                    "priority": 1,
                    "primary": True,
                    "enabled": True,
                    "geometry": {"x": 2560, "y": 0, "width": 1366, "height": 768},
                },
                {
                    "id": 2,
                    "name": "HDMI-A-1",
                    "priority": 2,
                    "primary": False,
                    "enabled": True,
                    "geometry": {"x": 0, "y": 0, "width": 2560, "height": 1440},
                },
            ],
            "windows": [
                {
                    "id": "firefox-1",
                    "title": "Docs — Mozilla Firefox",
                    "app_id": "firefox-bin",
                    "resource_class": "firefox",
                    "normal": True,
                    "special": False,
                    "geometry": {"x": 0, "y": 0, "width": 900, "height": 700},
                    "stacking_order": 2,
                },
                {
                    "id": "kitty-1",
                    "title": "~/Downloads",
                    "app_id": "kitty",
                    "resource_class": "kitty",
                    "normal": True,
                    "special": False,
                    "geometry": {"x": 100, "y": 100, "width": 800, "height": 600},
                    "stacking_order": 1,
                },
                {
                    "id": "internal",
                    "title": "",
                    "app_id": "",
                    "resource_class": "kwin_wayland",
                    "normal": True,
                    "special": False,
                    "geometry": {"x": 0, "y": 0, "width": 2560, "height": 1440},
                },
            ],
        }

    def test_resolves_natural_window_aliases_from_metadata(self) -> None:
        self.assertEqual(self.model.resolve_window("the browser", self.world)["id"], "firefox-1")
        self.assertEqual(self.model.resolve_window("my terminal", self.world)["id"], "kitty-1")
        self.assertEqual(self.model.resolve_window("this window", self.world)["id"], "firefox-1")

    def test_internal_kwin_surfaces_are_not_controllable_windows(self) -> None:
        ids = {item["id"] for item in self.model.visible_windows(self.world)}
        self.assertEqual(ids, {"firefox-1", "kitty-1"})

    def test_previous_exact_window_does_not_follow_active_or_replacement_window(self) -> None:
        self.assertEqual(
            self.model.resolve_window("window-id:kitty-1", self.world)["id"], "kitty-1"
        )
        with self.assertRaises(EntityResolutionError):
            self.model.resolve_window("window-id:closed-window", self.world)

    def test_resolves_monitor_topology(self) -> None:
        self.assertEqual(self.model.resolve_output("laptop screen", self.world)["name"], "eDP-1")
        self.assertEqual(self.model.resolve_output("other monitor", self.world)["name"], "eDP-1")
        self.assertEqual(
            self.model.resolve_output("second monitor", self.world)["name"], "HDMI-A-1"
        )
        self.assertEqual(self.model.resolve_output("left monitor", self.world)["name"], "HDMI-A-1")

    def test_resolves_virtual_desktops_by_position_name_and_context(self) -> None:
        self.assertEqual(self.model.resolve_desktop("workspace 2", self.world)["id"], "desktop-2")
        self.assertEqual(self.model.resolve_desktop("work", self.world)["id"], "desktop-2")
        self.assertEqual(
            self.model.resolve_desktop("current workspace", self.world)["id"], "desktop-1"
        )

    def test_ambiguous_match_fails_instead_of_guessing(self) -> None:
        duplicate = dict(self.world["windows"][1], id="kitty-2", stacking_order=1)
        self.world["windows"].append(duplicate)
        with self.assertRaises(EntityResolutionError):
            self.model.resolve_window("terminal", self.world)

    def test_long_description_does_not_fall_back_to_one_coincidental_token(self) -> None:
        ev = dict(
            self.world["windows"][0],
            id="ev-1",
            title="E.V. Control Center",
            app_id="ev-ui",
            resource_class="ev-ui",
        )
        self.world["windows"] = [ev]
        with self.assertRaises(EntityResolutionError):
            self.model.resolve_window("EV Phase 3 Disposable Terminal", self.world)

    def test_window_restore_history_is_bounded_and_defensive(self) -> None:
        original = self.world["windows"][0]
        for index in range(30):
            self.model.remember_window(original, f"move-{index}")
        restore = self.model.peek_window_restore()
        assert restore is not None
        self.assertEqual(restore["action"], "move-29")
        restore["window"]["title"] = "changed outside model"
        current = self.model.peek_window_restore()
        assert current is not None
        self.assertEqual(current["window"]["title"], original["title"])
        consumed = [self.model.consume_window_restore() for _ in range(24)]
        self.assertEqual(consumed[-1]["action"], "move-6")
        self.assertIsNone(self.model.consume_window_restore())


class DesktopActivationTests(unittest.IsolatedAsyncioTestCase):
    async def test_wayland_activation_falls_back_to_exact_kwin_runner_id(self) -> None:
        window_id = "{353181af-fe1a-46f9-aa46-c4de93913fdc}"
        inactive = {
            "id": window_id,
            "active": False,
            "normal": True,
            "special": False,
            "geometry": {"width": 100, "height": 100},
        }
        active = {**inactive, "active": True}
        model = SimpleNamespace(
            bridge=SimpleNamespace(request=AsyncMock(return_value={})),
            invalidate=Mock(),
            snapshot=AsyncMock(side_effect=[{"windows": [inactive]}, {"windows": [active]}]),
            visible_windows=lambda world: world["windows"],
        )
        runner = Mock(return_value={"ok": True, "stderr": ""})
        with (
            patch("ev.tools.builtin.run_command", runner),
            patch("ev.tools.builtin.asyncio.sleep", new=AsyncMock()),
        ):
            result = await desktop_activate_window(
                {"window_id": window_id}, SimpleNamespace(desktop=model)
            )

        self.assertTrue(result["verified"])
        self.assertEqual(result["activation_backend"], "kwin-window-runner")
        self.assertEqual(runner.call_args.args[0][-2:], [f"0_{window_id}", ""])


if __name__ == "__main__":
    unittest.main()
