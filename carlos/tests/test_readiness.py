import unittest
from ev.readiness import project_readiness


class ReadinessTests(unittest.TestCase):
    def test_installation_and_disabled_wake_never_claim_fully_ready(self):
        result = project_readiness(
            {"voice": {"stt_available": True, "tts_available": True}},
            {"LocalAI": {"state": "INSTALLED"}, "Remote": {"state": "READY"}},
            0.2,
        )
        self.assertEqual(result["state"], "PARTIAL")
        self.assertEqual(result["components"]["Voice"], "DISABLED")
        self.assertIsNone(result["boot_to_usable_seconds"])

    def test_only_observed_ready_components_allow_full_readiness(self):
        snapshot = {
            "voice": {
                "wake_enabled": True,
                "wake_active": True,
                "stt_available": True,
                "tts_available": True,
            },
            "desktop": {"available": True},
        }
        rows = {"LocalAI": {"state": "READY"}, "Remote": {"state": "READY"}}
        self.assertEqual(project_readiness(snapshot, rows)["state"], "FULLY_READY")
        snapshot["voice"]["privacy_mode"] = True
        self.assertEqual(project_readiness(snapshot, rows)["components"]["Voice"], "MUTED")
