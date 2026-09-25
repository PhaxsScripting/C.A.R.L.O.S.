import time
import unittest
from unittest.mock import AsyncMock

from ev.goals import validate_conditions, verify_conditions


class SettingsGoalTests(unittest.IsolatedAsyncioTestCase):
    def examples(self):
        return [
            (
                {"kind": "power_profile", "expected": "power-saver"},
                "settings.power_profile.get",
                {},
                {
                    "available": True,
                    "profiles": ["balanced", "power-saver"],
                    "current": "power-saver",
                },
            ),
            (
                {"kind": "screen_brightness", "expected": 40},
                "settings.brightness.get",
                {},
                {"percent": 40},
            ),
            (
                {
                    "kind": "default_application",
                    "mime_type": "application/pdf",
                    "expected": "okular.desktop",
                },
                "settings.default_application.get",
                {"mime_type": "application/pdf"},
                {"mime_type": "application/pdf", "desktop_id": "okular.desktop"},
            ),
            (
                {"kind": "night_light_state", "property": "enabled", "expected": False},
                "settings.night_light.inspect",
                {},
                {"available": True, "enabled": False},
            ),
        ]

    async def test_settings_predicates_only_request_corresponding_readonly_observation(self):
        for condition, name, arguments, data in self.examples():
            request = AsyncMock(
                return_value={
                    "status": "completed",
                    "result": {**data, "captured_at_monotonic": time.monotonic()},
                }
            )
            result = await verify_conditions([condition], request, "fixture")
            self.assertTrue(result["verified"], result)
            request.assert_awaited_once_with({"name": name, "arguments": arguments}, "fixture")

    async def test_settings_stale_missing_future_or_invalid_capture_never_verifies(self):
        for condition, _, _, data in self.examples():
            for captured in (
                None,
                True,
                "now",
                float("nan"),
                float("inf"),
                time.monotonic() - 3,
                time.monotonic() + 10,
            ):
                request = AsyncMock(
                    return_value={
                        "status": "completed",
                        "result": {**data, "captured_at_monotonic": captured},
                    }
                )
                self.assertFalse(
                    (await verify_conditions([condition], request, "fixture"))["verified"]
                )

    async def test_wrong_setting_unavailable_backend_or_wrong_mime_is_not_success(self):
        replacements = [
            [{"current": "balanced"}, {"available": False}, {"profiles": []}],
            [{"percent": 60}, {"percent": True}, {"percent": float("nan")}],
            [{"desktop_id": "firefox.desktop"}, {"mime_type": "text/plain"}, {"desktop_id": None}],
            [{"enabled": True}, {"available": False}, {"enabled": "false"}],
        ]
        for (condition, _, _, data), changes in zip(self.examples(), replacements):
            for change in changes:
                request = AsyncMock(
                    return_value={
                        "status": "completed",
                        "result": {**data, **change, "captured_at_monotonic": time.monotonic()},
                    }
                )
                self.assertFalse(
                    (await verify_conditions([condition], request, "fixture"))["verified"]
                )

    async def test_native_observation_failure_and_truncation_fail_closed(self):
        for condition, _, _, data in self.examples():
            for status, extra in (
                ("failed", {}),
                ("completed", {"truncated": True}),
                ("completed", {"error": "unavailable"}),
            ):
                request = AsyncMock(
                    return_value={
                        "status": status,
                        "result": {**data, **extra, "captured_at_monotonic": time.monotonic()},
                    }
                )
                self.assertFalse(
                    (await verify_conditions([condition], request, "fixture"))["verified"]
                )

    def test_invalid_predicates_reject_before_any_native_read(self):
        for condition in (
            {"kind": "power_profile", "expected": {"$ref": "observed.current"}},
            {"kind": "power_profile", "expected": "--help"},
            {"kind": "screen_brightness", "expected": True},
            {"kind": "screen_brightness", "expected": 101},
            {"kind": "default_application", "mime_type": "--help", "expected": "okular.desktop"},
            {
                "kind": "default_application",
                "mime_type": "application/pdf",
                "expected": "/tmp/okular.desktop",
            },
            {"kind": "night_light_state", "property": "temperature", "expected": 4000},
            {"kind": "night_light_state", "property": "enabled", "expected": "false"},
        ):
            with self.assertRaises(ValueError):
                validate_conditions([condition])

    async def test_night_light_conditions_share_one_fresh_native_observation(self):
        request = AsyncMock(
            return_value={
                "status": "completed",
                "result": {
                    "available": True,
                    "enabled": True,
                    "running": False,
                    "captured_at_monotonic": time.monotonic(),
                },
            }
        )
        result = await verify_conditions(
            [
                {"kind": "night_light_state", "property": "enabled", "expected": True},
                {"kind": "night_light_state", "property": "running", "expected": False},
            ],
            request,
            "fixture",
        )
        self.assertTrue(result["verified"])
        request.assert_awaited_once()
