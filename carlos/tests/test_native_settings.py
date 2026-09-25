import json
import unittest
from unittest.mock import patch

from ev.tools.native_settings import (
    power_profile_status,
    power_profile_set,
    display_status,
    night_light_status,
    association_get,
    association_set,
)


class NativeSettingsTests(unittest.TestCase):
    def test_empty_profile_backend_is_unavailable(self):
        with patch("ev.tools.native_settings.checked", return_value=""):
            self.assertFalse(power_profile_status({}, None)["available"])
            with self.assertRaises(ValueError):
                power_profile_set({"profile": "performance"}, None)

    def test_profile_change_is_exact_and_read_back(self):
        with patch(
            "ev.tools.native_settings.checked",
            side_effect=[
                "balanced\npower-saver",
                "balanced",
                "",
                "balanced\npower-saver",
                "power-saver",
            ],
        ) as command:
            result = power_profile_set({"profile": "power-saver"}, None)
        self.assertTrue(result["verified"])
        self.assertEqual(
            command.call_args_list[2].args[0][-2:],
            ["org.kde.Solid.PowerManagement.Actions.PowerProfile.setProfile", "power-saver"],
        )

    def test_profile_noop_does_not_write(self):
        with patch(
            "ev.tools.native_settings.checked", side_effect=["balanced", "balanced"]
        ) as command:
            self.assertTrue(power_profile_set({"profile": "balanced"}, None)["already_set"])
        self.assertEqual(command.call_count, 2)

    def test_failed_profile_readback_not_success(self):
        with patch(
            "ev.tools.native_settings.checked",
            side_effect=[
                "balanced\npower-saver",
                "balanced",
                "",
                "balanced\npower-saver",
                "balanced",
            ],
        ):
            self.assertFalse(power_profile_set({"profile": "power-saver"}, None)["verified"])

    def test_monitor_inventory_preserves_supported_modes_without_mutation(self):
        data = {
            "outputs": [
                {
                    "name": "HDMI-A-1",
                    "enabled": True,
                    "modes": [{"id": "1", "name": "1920x1080@120", "refreshRate": 120}],
                    "scale": 1.25,
                }
            ]
        }
        with patch("ev.tools.native_settings.checked", return_value=json.dumps(data)) as command:
            result = display_status({}, None)
        self.assertEqual(result["outputs"][0]["modes"][0]["refreshRate"], 120)
        self.assertFalse(result["changed"])
        command.assert_called_once_with(["/usr/bin/kscreen-doctor", "--json"], 4)

    def test_invalid_monitor_response_rejected(self):
        with patch("ev.tools.native_settings.checked", return_value="{}"), self.assertRaises(
            RuntimeError
        ):
            display_status({}, None)

    def test_night_light_readonly_properties(self):
        with patch(
            "ev.tools.native_settings.checked",
            side_effect=["true", "true", "false", "false", "6500", "4500"],
        ):
            result = night_light_status({}, None)
        self.assertTrue(result["enabled"])
        self.assertFalse(result["running"])
        self.assertEqual(result["targetTemperature"], 4500)

    def test_settings_capture_time_precedes_slow_multi_property_read(self):
        clock = [10.0]
        values = iter(["true", "true", "false", "false", "6500", "4500"])

        def read(*args):
            clock[0] += 1
            return next(values)

        with patch("ev.tools.native_settings.time.monotonic", side_effect=lambda: clock[0]), patch(
            "ev.tools.native_settings.checked", side_effect=read
        ):
            result = night_light_status({}, None)
        self.assertEqual(result["captured_at_monotonic"], 10.0)
        self.assertEqual(clock[0], 16.0)

    def test_association_stale_or_uninstalled_refused(self):
        arguments = {
            "mime_type": "application/pdf",
            "desktop_id": "okular",
            "expected_current": "old.desktop",
        }
        with patch("ev.tools.native_settings.desktop_entries", return_value={}), patch(
            "ev.tools.native_settings.checked"
        ) as command:
            with self.assertRaises(ValueError):
                association_set(arguments, None)
            command.assert_not_called()
        with patch("ev.tools.native_settings.desktop_entries", return_value={"okular": {}}), patch(
            "ev.tools.native_settings.checked", return_value="different.desktop"
        ) as command:
            with self.assertRaises(ValueError):
                association_set(arguments, None)
            self.assertEqual(command.call_count, 1)

    def test_association_exact_update_readback(self):
        with patch("ev.tools.native_settings.desktop_entries", return_value={"okular": {}}), patch(
            "ev.tools.native_settings.checked", side_effect=["old.desktop", "", "okular.desktop"]
        ) as command:
            result = association_set(
                {
                    "mime_type": "application/pdf",
                    "desktop_id": "okular",
                    "expected_current": "old.desktop",
                },
                None,
            )
        self.assertTrue(result["verified"])
        self.assertEqual(
            command.call_args_list[1].args[0],
            ["/usr/bin/xdg-mime", "default", "okular.desktop", "application/pdf"],
        )
