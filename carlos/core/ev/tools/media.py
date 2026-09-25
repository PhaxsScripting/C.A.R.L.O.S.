from __future__ import annotations

from ev.platform import executable as _platform_executable

import asyncio
import re
import time
from typing import Any

from .base import ToolContext


async def inspect_players(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Observe transport state without playback, launches or track metadata."""
    focus = context.media_focus
    requested = arguments.get("service", "")
    if requested and not re.fullmatch(
        r"org\.mpris\.MediaPlayer2\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*", requested
    ):
        raise ValueError("Use an exact observed MPRIS service name")
    if focus is None:
        return {
            "available": False,
            "players": [],
            "partial": True,
            "reason": "Media controller unavailable",
        }
    async with focus.lock:
        names = sorted(
            {
                line.strip()
                for line in (await focus._run(_platform_executable("/usr/bin/qdbus6"))).splitlines()
                if re.fullmatch(
                    r"org\.mpris\.MediaPlayer2\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*", line.strip()
                )
            }
        )
        if requested:
            names = [name for name in names if name == requested]
        partial = len(names) > 16
        semaphore = asyncio.Semaphore(4)

        async def observe(service):
            async with semaphore:
                try:
                    owner = await focus._owner(service)
                    if not re.fullmatch(r":[0-9]+\.[0-9]+", owner):
                        raise ValueError("Unbound media service")
                    status = await focus._property(owner, "PlaybackStatus")
                    if (
                        status not in {"Playing", "Paused", "Stopped"}
                        or await focus._owner(service) != owner
                    ):
                        raise ValueError("Media owner or state changed")
                    return {
                        "service": service,
                        "owner": owner,
                        "playback_status": status,
                        "captured_at_monotonic": time.monotonic(),
                    }
                except (OSError, ValueError, asyncio.TimeoutError):
                    return None

        rows = await asyncio.gather(*(observe(name) for name in names[:16]))
    return {
        "available": True,
        "players": [row for row in rows if row],
        "partial": partial or any(row is None for row in rows),
        "requested_service": requested or None,
        "backend": "MPRIS unique-owner PlaybackStatus readback",
        "changed": False,
        "note": "Current transport state only; no title, account, track contents, playback changes or app launch. Owner identity must still match for later verification.",
    }


def register_media_observation_tools(registry):
    from .base import ToolSpec
    from .builtin import object_schema
    from ..permissions import Permission

    registry.register(
        ToolSpec(
            "audio.players",
            "AUDIO",
            "Read existing media players' exact MPRIS services, unique bus owners and playback state. Optional exact service narrows inspection. Does not play, pause, launch apps or read track metadata. Use fresh service/owner for media_playback wait/verification conditions; do not assume the same app instance after a restart.",
            Permission.SAFE,
            object_schema({"service": {"type": "string", "maxLength": 200}}),
            inspect_players,
            read_only=True,
            timeout_seconds=20,
        )
    )


async def control_media(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    focus = context.media_focus
    action = arguments["action"]
    requested = str(arguments.get("player", "")).strip().casefold()
    launched = False

    def failure(message: str, **evidence: Any) -> dict[str, Any]:
        return {
            "ok": False,
            "verified": False,
            "action": action,
            "message": message,
            "reason": message,
            "launched": launched,
            **evidence,
        }

    if focus is None:
        return failure("The desktop media controller is unavailable.")
    if requested and not re.fullmatch(r"[a-z0-9_.-]{1,100}", requested):
        return failure("I need an exact media player name.")

    async def services() -> list[str]:
        found = sorted(
            {
                line.strip()
                for line in (await focus._run(_platform_executable("/usr/bin/qdbus6"))).splitlines()
                if line.strip().startswith("org.mpris.MediaPlayer2.")
            }
        )
        if requested:
            prefix = "org.mpris.MediaPlayer2." + requested
            found = [
                name
                for name in found
                if name.casefold() == prefix.casefold()
                or name.casefold().startswith(prefix.casefold() + ".")
            ]
        return found

    try:
        async with focus.lock:
            found = await services()
            if not found and requested == "spotify" and action == "play":
                from .builtin import open_application

                result = await asyncio.to_thread(
                    open_application, {"desktop_id": "com.spotify.Client"}, context
                )
                launched = bool(result.get("launched"))
                deadline = time.monotonic() + 8
                while launched and not found and time.monotonic() < deadline:
                    await asyncio.sleep(0.2)
                    found = await services()
            if not found:
                return failure(
                    f"{requested.title() if requested else 'A compatible media player'} is not open or available for playback control."
                )
            if len(found) > 16:
                return failure("Several media players are open. Tell me which player to use.")
            semaphore = asyncio.Semaphore(4)

            async def inspect(service: str) -> dict[str, str] | None:
                async with semaphore:
                    try:
                        owner = await focus._owner(service)
                        if not owner.startswith(":"):
                            return None
                        status = await focus._property(owner, "PlaybackStatus")
                        return {"service": service, "owner": owner, "status": status}
                    except (OSError, asyncio.TimeoutError):
                        return None

            rows = [
                row for row in await asyncio.gather(*(inspect(service) for service in found)) if row
            ]
            if len(rows) != len(found):
                return failure(
                    "A media player stopped responding. I couldn't reliably select the requested playback."
                )
            if not requested:
                active = [
                    row
                    for row in rows
                    if row["status"] == "Playing"
                    or focus.players.get(row["service"], ("", ""))[0] == row["owner"]
                ]
                if active:
                    rows = active
                elif action == "play":
                    paused = [row for row in rows if row["status"] == "Paused"]
                    if paused:
                        rows = paused
            if len(rows) != 1:
                names = [row["service"].removeprefix("org.mpris.MediaPlayer2.") for row in rows]
                return failure(
                    "Several players match. Name the player, for example pause Spotify.",
                    candidates=names,
                )
            row = rows[0]
            service, owner = row["service"], row["owner"]
            label = (
                "Spotify"
                if service.casefold().startswith("org.mpris.mediaplayer2.spotify")
                else service.removeprefix("org.mpris.MediaPlayer2.").split(".")[0].title()
            )
            before_metadata = (
                await focus._property(owner, "Metadata") if action in {"next", "previous"} else ""
            )
            expected = {
                "play": "Playing",
                "pause": "Paused",
                "toggle": (
                    "Paused"
                    if row["status"] == "Playing"
                    or focus.players.get(service, ("", ""))[0] == owner
                    else "Playing"
                ),
            }.get(action)
            method = {
                "play": "Play",
                "pause": "Pause",
                "toggle": "Pause" if expected == "Paused" else "Play",
                "next": "Next",
                "previous": "Previous",
            }[action]
            if await focus._owner(service) != owner:
                return failure("The media player changed before I could control it.")
            if row["status"] != expected:
                await focus._control(owner, method)
            verified = False
            status = row["status"]
            metadata = before_metadata
            for attempt in range(5):
                if attempt:
                    await asyncio.sleep(0.08)
                status = await focus._property(owner, "PlaybackStatus")
                if expected:
                    verified = status == expected
                else:
                    metadata = await focus._property(owner, "Metadata")
                    verified = bool(metadata) and metadata != before_metadata
                if verified:
                    break
            if not verified:
                return failure(
                    f"{label} received the request, but I couldn't verify the playback change.",
                    player=service,
                    playback_status=status,
                    command_sent=True,
                )
            if await focus._owner(service) != owner:
                return failure(
                    "The media player was replaced during playback verification; the new instance was not controlled.",
                    player=service,
                    owner=owner,
                    command_sent=True,
                )
            held = focus.players.get(service)
            if held and held[0] == owner:
                if expected:
                    focus.players.pop(service, None)
                else:
                    focus.players[service] = (owner, metadata)
            message = (
                f"{label} is {status.lower()}."
                if expected
                else f"Moved to the {action} track in {label}."
            )
            return {
                "ok": True,
                "verified": True,
                "action": action,
                "player": service,
                "owner": owner,
                "playback_status": status,
                "message": message,
                "launched": launched,
                "captured_at_monotonic": time.monotonic(),
            }
    except (OSError, ValueError, asyncio.TimeoutError) as error:
        return failure(f"Playback control failed: {type(error).__name__}.")
