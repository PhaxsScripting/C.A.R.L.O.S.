import time
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from ev.desktop.observation import DesktopObservation
from ev.desktop.world import DesktopWorldModel


class DesktopObservationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.window = {
            "id": "exact",
            "pid": 123,
            "title": "Test app",
            "normal": True,
            "geometry": {"x": 0, "y": 0, "width": 400, "height": 300},
        }
        self.world = {
            "active_window_id": "exact",
            "windows": [self.window],
            "outputs": [],
            "cursor": {"x": 100, "y": 100},
            "captured_at_monotonic": time.monotonic(),
        }
        self.desktop = SimpleNamespace(
            snapshot=AsyncMock(return_value=self.world),
            visible_windows=DesktopWorldModel.visible_windows,
            input=SimpleNamespace(
                status=Mock(return_value={"available": True, "connected": False})
            ),
        )
        self.accessibility = SimpleNamespace(
            list_elements=Mock(return_value={"elements": [{"name": "Save"}], "truncated": False})
        )
        self.observer = DesktopObservation(self.desktop, self.accessibility)

    async def test_basic_observation_does_not_scan_accessibility(self):
        result = await self.observer.observe()
        self.assertEqual(result["target_window"]["id"], "exact")
        self.assertEqual(result["warnings"][0]["code"], "INPUT_DISCONNECTED")
        self.assertFalse(result["capture_performed"])
        self.accessibility.list_elements.assert_not_called()
        self.desktop.snapshot.assert_awaited_once_with(force=True)

    async def test_accessibility_is_bound_to_pid_and_title_then_rechecked(self):
        result = await self.observer.observe(level="accessibility", limit=20)
        self.accessibility.list_elements.assert_called_once_with("", "", "", 20, 123, "Test app")
        self.assertEqual(self.desktop.snapshot.await_count, 2)
        self.assertEqual(result["accessibility"]["status"], "OBSERVED")

    async def test_focus_changed_drops_controls_for_implicit_active_target(self):
        after = deepcopy(self.world)
        after["active_window_id"] = "other"
        self.desktop.snapshot.side_effect = [self.world, after]
        result = await self.observer.observe(level="accessibility")
        self.assertEqual(result["accessibility"]["status"], "STALE")
        self.assertNotIn("elements", result["accessibility"])

    async def test_explicit_target_can_be_inspected_without_stealing_focus(self):
        self.world["active_window_id"] = "other"
        result = await self.observer.observe(window_id="exact", level="accessibility")
        self.assertEqual(result["accessibility"]["status"], "OBSERVED")

    async def test_title_or_pid_changed_discards_stale_elements(self):
        for field, value in (("title", "Different document"), ("pid", 999)):
            with self.subTest(field=field):
                after = deepcopy(self.world)
                after["windows"][0][field] = value
                self.desktop.snapshot.side_effect = [self.world, after]
                result = await self.observer.observe(window_id="exact", level="accessibility")
                self.assertEqual(result["accessibility"]["status"], "STALE")

    async def test_missing_explicit_target_does_not_switch_to_active(self):
        result = await self.observer.observe(window_id="missing", level="accessibility")
        self.assertFalse(result["ok"])
        self.accessibility.list_elements.assert_not_called()

    async def test_empty_and_truncated_results_are_not_reported_complete(self):
        for listing, status in (
            ({"elements": []}, "EMPTY"),
            ({"elements": [{"name": "Save"}], "truncated": True}, "PARTIAL"),
        ):
            self.accessibility.list_elements.return_value = listing
            result = await self.observer.observe(level="accessibility")
            self.assertEqual(result["accessibility"]["status"], status)

    async def test_accessibility_failure_preserves_basic_observation(self):
        self.accessibility.list_elements.side_effect = RuntimeError("AT-SPI unavailable")
        result = await self.observer.observe(level="accessibility")
        self.assertTrue(result["ok"])
        self.assertEqual(result["accessibility"]["status"], "UNAVAILABLE")
        self.assertEqual(result["target_window"]["id"], "exact")

    async def test_cached_observation_requires_explicit_opt_in(self):
        await self.observer.observe(fresh=False)
        self.desktop.snapshot.assert_awaited_once_with(force=False)

    async def test_invalid_level_or_limit_never_queries_desktop(self):
        for arguments in ({"level": "screenshot"}, {"limit": 151}, {"limit": 0}):
            with self.assertRaises(ValueError):
                await self.observer.observe(**arguments)
        self.desktop.snapshot.assert_not_awaited()
