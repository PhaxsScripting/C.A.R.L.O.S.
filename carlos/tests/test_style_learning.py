import tempfile
import unittest
from unittest.mock import Mock
from pathlib import Path

from ev.daily import DailyStore
from ev.style import StyleLearner
from ev.tools.preferences import context_records


class StyleLearningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DailyStore(Path(self.temp.name) / "daily.db")
        self.learner = StyleLearner(self.store)

    def test_repeated_feedback_learns_gradually_without_saving_utterances(self):
        for text in ("shorter please", "Can you be brief?", "Keep your replies short."):
            result = self.learner.observe_user_feedback(text)
        self.assertEqual(result["response_length_hint"], "short")
        self.assertEqual(result["feedback_count"], 3)
        self.assertEqual(
            set(self.store.records("learned_style")["response_length"]),
            {"score", "feedback_count", "enabled"},
        )
        self.assertEqual(StyleLearner(DailyStore(self.store.path)).snapshot()["score"], -3)

    def test_quoted_feedback_ordinary_commands_and_negation_do_not_learn(self):
        for text in (
            'The website says "be brief"',
            "Do not keep it short",
            "shorter password please",
            "Open Firefox and be brief",
            "I like short stories",
            "be concise; delete everything",
        ):
            self.learner.observe_user_feedback(text)
        self.assertEqual(self.learner.snapshot()["feedback_count"], 0)
        self.assertEqual(self.store.records("learned_style"), {})

    def test_hint_changes_with_feedback_without_inventing_slang_or_authority(self):
        for _ in range(3):
            self.learner.observe_user_feedback("shorter")
        for _ in range(6):
            self.learner.observe_user_feedback("more detail")
        hint = context_records("Explain compilers", self.store, {}, learn_style=True)
        self.assertIn("prefer detailed", str(hint))
        self.assertIn("not an instruction to execute", str(hint))
        self.store.save("preference", "response_length", {"value": "short"})
        self.assertNotIn(
            "learned-response-style",
            str(context_records("Explain compilers", self.store, {}, learn_style=True)),
        )

    def test_disabled_and_reset_preserve_explicit_preferences(self):
        self.store.save("preference", "browser", {"value": "firefox"})
        self.learner.observe_user_feedback("more detail")
        self.learner.configure(enabled=False)
        self.learner.observe_user_feedback("more detail")
        self.assertEqual(self.learner.snapshot()["feedback_count"], 1)
        self.learner.configure(reset=True)
        self.assertEqual(self.learner.snapshot()["feedback_count"], 0)
        self.assertFalse(self.learner.snapshot()["enabled"])
        self.assertEqual(self.store.records("preference")["browser"]["value"], "firefox")

    def test_observing_context_requires_explicit_trusted_user_entry_point(self):
        context_records("shorter", self.store, {})
        self.assertEqual(self.learner.snapshot()["feedback_count"], 0)

    def test_failed_persistence_never_reports_style_configured(self):
        store = Mock(records=Mock(return_value={}))
        with self.assertRaises(RuntimeError):
            StyleLearner(store).configure(enabled=False)


class StyleIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_user_context_learns_but_explicit_personality_prevents_override(self):
        from ev.paths import Paths
        from ev.service import CarlosCore

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = CarlosCore(
                paths=Paths(*(root / n for n in ("config", "data", "state", "cache", "runtime")))
            )
            try:
                for _ in range(3):
                    records = await service._model_context("shorter")
                self.assertIn("learned-response-style", str(records))
                service.config["personality"]["response_length"] = "detailed"
                self.assertNotIn(
                    "learned-response-style", str(await service._model_context("shorter"))
                )
                self.assertEqual(StyleLearner(service.daily).snapshot()["feedback_count"], 3)
                service.tools.validate("personalization.style_learning", {"enabled": False})
                result = await service.request_tool(
                    {"name": "personalization.style_learning", "arguments": {"enabled": False}},
                    "style-test",
                )
                self.assertTrue(result["result"]["verified"])
                self.assertFalse(result["result"]["style"]["enabled"])
            finally:
                service.memory.close()
