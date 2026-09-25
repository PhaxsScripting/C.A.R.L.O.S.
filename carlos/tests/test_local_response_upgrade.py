import unittest
from unittest.mock import AsyncMock

from ev.ai.local_llama import LocalHybridProvider, LocalLlamaProvider, CASUAL_STYLE
from ev.ai.base import ProviderTurn
from ev.commands import direct_action
from ev.voice.normalization import is_negative_action, is_conversation_stop


class LocalResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_general_questions_do_not_send_desktop_schemas(self):
        provider = LocalHybridProvider({"model": "test"})
        provider.local.begin = AsyncMock(
            return_value=ProviderTurn("local_llama", "test", "A useful answer.")
        )
        tools = [
            {"name": "audio.media", "permission": "SAFE"},
            {"name": "system.get_memory_usage", "permission": "SAFE"},
        ]
        for question in (
            "Is listening to music good while coding?",
            "Is more RAM always better?",
            "What makes music relaxing?",
        ):
            await provider.begin(question, [], [], tools)
            self.assertEqual(provider.local.begin.await_args.args[-1], [], question)

    def test_real_desktop_requests_retain_relevant_tools(self):
        tools = [
            {"name": "audio.media", "permission": "SAFE"},
            {"name": "audio.get_volume", "permission": "SAFE"},
        ]
        self.assertTrue(LocalHybridProvider._relevant_tools("pause my music", [], tools))
        self.assertTrue(LocalHybridProvider._relevant_tools("what is my volume", [], tools))

    def test_brief_replies_do_not_shorten_explanations_or_multiple_questions(self):
        provider = LocalLlamaProvider({})
        for text in (
            "Why does garlic burn?",
            "How do I cook pasta?",
            "What is it? Is it useful?",
            "Compare those options",
            "List the steps",
        ):
            self.assertEqual(
                provider._reply_sentence_limit([{"role": "user", "content": text + CASUAL_STYLE}]),
                2,
            )
        self.assertEqual(
            provider._reply_sentence_limit(
                [{"role": "user", "content": "Pasta sounds nice." + CASUAL_STYLE}]
            ),
            1,
        )

    def test_stop_requests_are_fast_but_normal_replies_still_continue(self):
        for phrase in ("Alright, enough. Good.", "that's enough", "stop responding please"):
            self.assertTrue(is_conversation_stop(phrase), phrase)
        for phrase in ("thanks", "good", "I have enough pasta", "what is enough RAM?"):
            self.assertFalse(is_conversation_stop(phrase), phrase)

    def test_not_knowing_is_not_a_command_negation(self):
        self.assertFalse(is_negative_action("I don't know the name. Just open it."))
        self.assertFalse(is_negative_action("I don't know how to open Firefox"))
        for phrase in (
            "don't open Firefox",
            "do not pause Spotify",
            "I don't think you should open Firefox",
            "never close Firefox",
        ):
            self.assertTrue(is_negative_action(phrase), phrase)

    def test_natural_playback_requests_bypass_model(self):
        for phrase, action in (
            ("get Spotify playing again", "play"),
            ("put my music back on", "play"),
            ("pause playback on Spotify", "pause"),
            ("unpause playback in Spotify", "play"),
        ):
            selected = direct_action(phrase)
            self.assertEqual(selected.tool, "audio.media", phrase)
            self.assertEqual(selected.arguments["action"], action)
        for phrase in (
            "don't get Spotify playing again",
            "explain how to put music back on",
            "get Spotify playing again if I say yes",
        ):
            self.assertIsNone(direct_action(phrase), phrase)
