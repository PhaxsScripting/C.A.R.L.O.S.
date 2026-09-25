import unittest

from ev.capabilities import capability_evidence


class CapabilityEvidenceTests(unittest.TestCase):
    def test_registered_tool_is_not_claimed_working(self):
        row = capability_evidence([{"name": "settings.power_profile.set"}], [], {})[0]
        self.assertEqual(row["implementation"], "REGISTERED")
        self.assertIsNone(row["backend_state"]["available"])
        self.assertIsNone(row["backend_state"]["functioning"])
        self.assertIsNone(row["execution_evidence"]["verification_fraction"])

    def test_portal_grant_and_other_tool_success_do_not_verify_keyboard(self):
        row = capability_evidence(
            [{"name": "desktop.keyboard.type"}],
            [
                {
                    "type": "tool.completed",
                    "payload": {
                        "tool": "desktop.pointer.move",
                        "ok": True,
                        "execution": {"verified": True},
                    },
                }
            ],
            {"connected": True, "authorized": True, "functioning": True},
        )[0]
        self.assertTrue(row["backend_state"]["connected"])
        self.assertIsNone(row["backend_state"]["functioning"])
        self.assertEqual(row["execution_evidence"]["observations"], 0)

    def test_handoff_and_failure_not_counted_as_verified_effect(self):
        history = [
            {
                "type": "tool.completed",
                "payload": {
                    "tool": "browser.open_url",
                    "ok": True,
                    "execution": {"verified": False, "status": "EXECUTED_UNVERIFIED"},
                },
            },
            {"type": "tool.failed", "payload": {"tool": "browser.open_url", "error": "missing"}},
        ]
        evidence = capability_evidence([{"name": "browser.open_url"}], history, {})[0][
            "execution_evidence"
        ]
        self.assertEqual(evidence["accepted_or_completed"], 1)
        self.assertEqual(evidence["failures"], 1)
        self.assertEqual(evidence["verified_results"], 0)
        self.assertIsNone(evidence["independent_goal_success_rate"])

    def test_read_observation_is_not_current_availability_or_goal_success(self):
        row = capability_evidence(
            [{"name": "settings.power_profile.get"}],
            [
                {
                    "type": "tool.completed",
                    "payload": {
                        "tool": "settings.power_profile.get",
                        "ok": True,
                        "execution": {"verified": True, "scope": "observation_only"},
                        "result": {"available": False},
                    },
                }
            ],
            {},
        )[0]
        self.assertFalse(row["backend_state"]["last_reported_available"])
        self.assertIsNone(row["backend_state"]["available"])
        self.assertEqual(row["execution_evidence"]["last_verification_scope"], "observation_only")
