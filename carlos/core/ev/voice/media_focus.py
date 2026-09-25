"""Reversible, conversation-scoped media interruption. Never changes a sink."""

from __future__ import annotations

from ev.platform import executable as _platform_executable

import asyncio
from typing import Any, Awaitable, Callable


class MediaFocus:
    def __init__(
        self,
        listing: Callable[[str], Awaitable[list[dict[str, Any]]]],
        environment: Callable[[], dict[str, str]],
    ) -> None:
        self.listing = listing
        self.environment = environment
        self.players: dict[str, tuple[str, str]] = {}
        self.streams: dict[str, tuple[str, str, str, str]] = {}
        self.active = False
        self.lock = asyncio.Lock()

    async def preserve_explicit_mute(self, index: str, identity: list[str]) -> None:
        """Do not undo an explicit app mute/unmute after our temporary silence."""
        async with self.lock:
            if self.streams.get(index) == tuple(identity):
                self.streams.pop(index, None)

    async def _run(self, *args: str) -> str:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=self.environment(),
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 0.8)
            if process.returncode:
                raise OSError("Media control unavailable")
            return output.decode(errors="replace").strip()
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()

    async def _owner(self, service: str) -> str:
        return await self._run(
            _platform_executable("/usr/bin/qdbus6"),
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus.GetNameOwner",
            service,
        )

    async def _property(self, owner: str, name: str) -> str:
        return await self._run(
            _platform_executable("/usr/bin/qdbus6"),
            owner,
            "/org/mpris/MediaPlayer2",
            "org.freedesktop.DBus.Properties.Get",
            "org.mpris.MediaPlayer2.Player",
            name,
        )

    async def _control(self, owner: str, action: str) -> str:
        return await self._run(
            _platform_executable("/usr/bin/qdbus6"),
            owner,
            "/org/mpris/MediaPlayer2",
            f"org.mpris.MediaPlayer2.Player.{action}",
        )

    @staticmethod
    def _identity(stream: dict[str, Any]) -> tuple[str, str, str, str]:
        properties = stream.get("properties", {})
        return tuple(
            str(value)
            for value in (
                stream.get("client", ""),
                properties.get("application.process.id", ""),
                properties.get("application.name", ""),
                properties.get("media.name", ""),
            )
        )

    async def pause(self) -> None:
        async with self.lock:
            if self.active:
                return
            self.active = True
            try:
                streams = await self.listing("sink-inputs")
            except (OSError, ValueError, asyncio.TimeoutError):
                streams = []
            # Silence existing external playback immediately; never mute the
            # output device, microphone, already-muted streams or E.V. speech.
            for stream in streams:
                identity = self._identity(stream)
                if (
                    stream.get("corked", True)
                    or stream.get("mute", False)
                    or identity[2] == "E.V."
                    or identity[3] == "E.V. speech"
                ):
                    continue
                index = str(stream.get("index", ""))
                if not index.isdigit():
                    continue
                try:
                    # Record before mutation so cancellation still restores it.
                    self.streams[index] = identity
                    await self._run(
                        _platform_executable("/usr/bin/pactl"), "set-sink-input-mute", index, "1"
                    )
                except (OSError, asyncio.TimeoutError):
                    pass
            try:
                services = (await self._run(_platform_executable("/usr/bin/qdbus6"))).splitlines()
            except (OSError, asyncio.TimeoutError):
                return
            semaphore = asyncio.Semaphore(4)

            async def pause_player(service: str) -> None:
                async with semaphore:
                    try:
                        owner = await self._owner(service)
                        if (
                            not owner.startswith(":")
                            or await self._property(owner, "PlaybackStatus") != "Playing"
                        ):
                            return
                        metadata = await self._property(owner, "Metadata")
                        self.players[service] = (owner, metadata)
                        await self._control(owner, "Pause")
                    except (OSError, asyncio.TimeoutError):
                        pass

            await asyncio.gather(
                *(
                    pause_player(service)
                    for service in [
                        s.strip()
                        for s in services
                        if s.strip().startswith("org.mpris.MediaPlayer2.")
                    ][:16]
                )
            )

    async def restore(self, *, resume: bool = True) -> None:
        async with self.lock:
            if not self.active:
                return
            try:
                current = {str(s.get("index")): s for s in await self.listing("sink-inputs")}
            except (OSError, ValueError, asyncio.TimeoutError):
                # Keep restoration records for a retry if the audio service
                # is temporarily unavailable; absence is not disconnection.
                return
            for index, identity in list(self.streams.items()):
                stream = current.get(index)
                if stream and self._identity(stream) == identity and stream.get("mute", False):
                    try:
                        await self._run(
                            _platform_executable("/usr/bin/pactl"),
                            "set-sink-input-mute",
                            index,
                            "0",
                        )
                    except (OSError, asyncio.TimeoutError):
                        continue
                self.streams.pop(index, None)
            for service, (owner, metadata) in list(self.players.items()):
                if resume:
                    try:
                        # Never restart a replacement player, another track,
                        # stopped media, or something the user already resumed.
                        if (
                            await self._owner(service) == owner
                            and await self._property(owner, "PlaybackStatus") == "Paused"
                            and await self._property(owner, "Metadata") == metadata
                        ):
                            await self._control(owner, "Play")
                    except (OSError, asyncio.TimeoutError):
                        continue
                self.players.pop(service, None)
            self.active = bool(self.players or self.streams)
