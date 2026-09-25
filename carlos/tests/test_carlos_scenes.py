import tempfile, unittest
from types import SimpleNamespace
from pathlib import Path
from ev.scenes import SceneEngine
from ev.daily import DailyStore
from ev.events import PhaxEventBus


class SceneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.core = SimpleNamespace(
            daily=DailyStore(Path(self.tmp.name) / "daily.db"), bus=PhaxEventBus()
        )
        self.scene = SceneEngine(self.core)

    def tearDown(self):
        self.tmp.cleanup()

    def test_negation_and_discussion_never_activate(self):
        for text in ("don't activate coding mode", "explain coding mode", "what is focus mode?"):
            self.assertIsNone(self.scene.resolve(text))
        self.assertEqual(self.scene.resolve("activate coding mode"), "coding")

    def test_defaults_do_not_launch_or_change_desktop(self):
        definition = self.scene.activate("homecoming")
        self.assertEqual(definition["commands"], [])
        self.assertEqual(self.scene.current["hud"], "CARLOS")
        with self.assertRaises(ValueError):
            self.scene.activate("invented")

    def test_custom_plan_is_explicit_and_persistent(self):
        self.core.daily.save(
            "carlos_scene", "work", {"commands": ["open Firefox"], "hud": "PROJECT", "quiet": True}
        )
        self.assertEqual(self.scene.definitions()["work"]["commands"], ["open Firefox"])

    def test_failed_or_stale_execution_never_claims_active(self):
        self.scene.activate("coding", running=True)
        original = self.scene.current
        self.assertEqual(original["state"], "RUNNING")
        self.scene.finish(original, "failed")
        self.assertEqual(original["state"], "FAILED")
        self.scene.activate("gaming")
        self.scene.finish(original, "completed")
        self.assertEqual(self.scene.current["name"], "gaming")
