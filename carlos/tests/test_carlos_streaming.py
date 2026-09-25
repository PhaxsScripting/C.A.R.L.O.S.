import asyncio, unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from ev.voice.streaming import PartialTranscript, speech_chunks


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_preview_is_discarded_and_only_one_inflight(self):
        release = asyncio.Event()
        seen = []

        async def transcribe(pcm):
            await release.wait()
            return SimpleNamespace(raw="private preview")

        transcribe = AsyncMock(side_effect=transcribe)
        partial = PartialTranscript(transcribe, lambda *args: seen.append(args))
        partial.feed(b"\0" * 64000, "old")
        partial.feed(b"\0" * 64000, "old")
        await asyncio.sleep(0)
        cleanup = asyncio.create_task(partial.finish())
        await asyncio.sleep(0)
        release.set()
        await cleanup
        self.assertEqual(seen, [])
        self.assertEqual(transcribe.await_count, 1)

    def test_sentence_chunks_preserve_words_and_bound_initial_synthesis(self):
        text = (
            "Hello there. " + ("This is a longer sentence with several words " * 10).strip() + "."
        )
        chunks = speech_chunks(text)
        self.assertEqual(chunks[0], "Hello there.")
        self.assertEqual(" ".join(chunks), text)
        self.assertTrue(all(len(s) <= 180 for s in chunks))
