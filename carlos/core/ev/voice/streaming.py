"""Bounded speech chunks and advisory partial transcription."""

import asyncio
import re
import time


def speech_chunks(text, limit=180):
    chunks = []
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        while len(sentence) > limit:
            end = sentence.rfind(" ", 0, limit)
            end = end if end > 0 else limit
            chunks.append(sentence[:end])
            sentence = sentence[end:].lstrip()
        if sentence:
            chunks.append(sentence)
    return chunks or [text]


class PartialTranscript:
    """One local preview at a time; previews never execute commands."""

    def __init__(self, transcribe, emit):
        self.transcribe, self.emit = transcribe, emit
        self.task = None
        self.next_at = 0.0
        self.generation = 0

    def feed(self, pcm, correlation):
        if (
            len(pcm) < 48000
            or time.monotonic() < self.next_at
            or self.task
            and not self.task.done()
        ):
            return
        self.next_at = time.monotonic() + 2.5
        generation = self.generation
        sample = bytes(pcm[-192000:])

        async def preview():
            started = time.monotonic()
            try:
                result = await self.transcribe(sample)
                if generation == self.generation:
                    self.emit(result.raw, correlation, (time.monotonic() - started) * 1000)
            except (OSError, RuntimeError, TimeoutError):
                pass

        self.task = asyncio.create_task(preview())

    async def finish(self):
        self.generation += 1
        task, self.task = self.task, None
        self.next_at = 0.0
        if task and not task.done():
            # Let a near-complete preview release the persistent STT worker.
            # A stalled preview cannot delay final recognition indefinitely.
            try:
                await asyncio.wait_for(task, 0.25)
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            except asyncio.CancelledError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
