"""Bounded sentence-to-audio stream owned by one active reasoning request."""

import asyncio
from contextvars import ContextVar

desktop_speech = ContextVar("carlos_desktop_speech", default=True)


class ReplyStreams:
    def __init__(self, core):
        self.core = core
        self.entries = {}

    def feed(self, correlation, text, failed=False):
        entry = self.entries.get(correlation)
        if text is None:
            if entry:
                if failed:
                    entry["task"].cancel()
                else:
                    entry["closed"] = True
            return
        if (
            not desktop_speech.get()
            or not self.core.config["assistant"].get("speak_responses", True)
            or self.core.voice.privacy_mode
        ):
            return
        if entry is None:
            if len(self.entries) >= 8:
                return
            queue = asyncio.Queue(maxsize=16)
            state = {"queue": queue, "closed": False, "failed_before_playback": False}

            async def remainder():
                while True:
                    if self.core.voice.tts_cancel_reason or (state["closed"] and queue.empty()):
                        break
                    try:
                        sentence = await asyncio.wait_for(queue.get(), 0.1)
                    except TimeoutError:
                        continue
                    if sentence is None:
                        break
                    yield sentence

            async def play():
                try:
                    await self.core.voice.speak(
                        text, correlation, allow_follow_up=True, continuation=remainder()
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    # Retry the final response only when no audio started. A
                    # partial spoken reply must not be replayed from the top.
                    state["failed_before_playback"] = not any(
                        event.get("type") == "tts.started"
                        and event.get("correlation_id") == correlation
                        for event in self.core.bus.history()
                    )
                    self.core.bus.publish(
                        "tts.stream_failed", "voice", {"error": str(error)}, correlation
                    )

            task = asyncio.create_task(play())
            self.core._background_tasks.add(task)
            task.add_done_callback(self.core._background_tasks.discard)
            state["task"] = task
            self.entries[correlation] = state
        elif entry["closed"]:
            return
        elif not entry["queue"].full():
            entry["queue"].put_nowait(text)
        else:
            entry["task"].cancel()

    async def finish(self, correlation):
        entry = self.entries.get(correlation)
        if entry:
            await asyncio.gather(entry["task"], return_exceptions=True)

    def consumed(self, correlation):
        entry = self.entries.pop(correlation, None)
        return entry is not None and not entry.get("failed_before_playback", False)

    def cancel(self):
        for entry in self.entries.values():
            entry["task"].cancel()
        self.entries.clear()
