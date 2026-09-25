import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from ev.voice.reply_stream import ReplyStreams, desktop_speech
from ev.events import PhaxEventBus


class ReplyStreamTests(unittest.IsolatedAsyncioTestCase):
    def core(self):
        return SimpleNamespace(
            config={"assistant": {"speak_responses": True}},
            voice=SimpleNamespace(privacy_mode=False, tts_cancel_reason="", speak=AsyncMock()),
            bus=PhaxEventBus(),
            _background_tasks=set(),
        )

    async def test_first_sentence_plays_before_generation_finishes_without_duplicate(self):
        core = self.core()
        heard = []
        started = asyncio.Event()

        async def speak(text, correlation, continuation, allow_follow_up):
            self.assertTrue(allow_follow_up)
            heard.append(text)
            started.set()
            async for text in continuation:
                heard.append(text)

        core.voice.speak.side_effect = speak
        streams = ReplyStreams(core)
        streams.feed("one", "First sentence.")
        await started.wait()
        self.assertEqual(heard, ["First sentence."])
        streams.feed("one", "Second sentence.")
        streams.feed("one", None)
        await streams.finish("one")
        self.assertEqual(heard, ["First sentence.", "Second sentence."])
        self.assertTrue(streams.consumed("one"))
        self.assertFalse(streams.consumed("one"))

    async def test_mobile_routing_and_privacy_do_not_start_desktop_audio(self):
        core = self.core()
        streams = ReplyStreams(core)
        token = desktop_speech.set(False)
        try:
            streams.feed("phone", "Private phone reply.")
        finally:
            desktop_speech.reset(token)
        core.voice.privacy_mode = True
        streams.feed("muted", "Muted reply.")
        await asyncio.sleep(0)
        core.voice.speak.assert_not_called()

    async def test_failed_generation_cancels_pending_audio(self):
        core = self.core()
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def speak(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        core.voice.speak.side_effect = speak
        streams = ReplyStreams(core)
        streams.feed("one", "First sentence.")
        await started.wait()
        streams.feed("one", None, True)
        await streams.finish("one")
        self.assertTrue(stopped.is_set())

    async def test_full_queue_finishes_without_losing_sentences(self):
        core = self.core()
        heard = []

        async def speak(text, correlation, continuation, allow_follow_up):
            heard.append(text)
            async for sentence in continuation:
                heard.append(sentence)

        core.voice.speak.side_effect = speak
        streams = ReplyStreams(core)
        streams.feed("full", "first")
        for index in range(16):
            streams.feed("full", str(index))
        streams.feed("full", None)
        streams.feed("full", "late sentence")
        await asyncio.wait_for(streams.finish("full"), 1)
        self.assertEqual(heard, ["first"] + [str(index) for index in range(16)])

    async def test_failure_before_playback_allows_final_response_fallback(self):
        core = self.core()
        core.voice.speak.side_effect = RuntimeError("Cannot speak while busy")
        streams = ReplyStreams(core)
        streams.feed("retry", "First sentence.")
        streams.feed("retry", None)
        await streams.finish("retry")
        self.assertFalse(streams.consumed("retry"))

    async def test_failure_after_playback_does_not_repeat_spoken_reply(self):
        core = self.core()

        async def speak(*args, **kwargs):
            core.bus.publish("tts.started", "voice", {}, "partial")
            raise RuntimeError("Playback failed after starting")

        core.voice.speak.side_effect = speak
        streams = ReplyStreams(core)
        streams.feed("partial", "First sentence.")
        streams.feed("partial", None)
        await streams.finish("partial")
        self.assertTrue(streams.consumed("partial"))
