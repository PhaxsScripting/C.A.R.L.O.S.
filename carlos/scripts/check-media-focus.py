#!/usr/bin/env python3
"""Explicit, short media pause/mute and restoration check. No microphone use."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from ev.events import PhaxEventBus
from ev.state import StateMachine
from ev.voice.manager import VoiceManager


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply", action="store_true", help="Actually interrupt current playback for two seconds"
    )
    args = parser.parse_args()
    if not args.apply:
        parser.error("Use --apply to authorize the brief playback check")
    bus = PhaxEventBus()
    manager = VoiceManager({"wake": {"enabled": False}}, bus, StateMachine(bus))
    before = await manager._audio_listing("sink-inputs")
    try:
        await manager.media_focus.pause()
        held = await manager._audio_listing("sink-inputs")
        print(
            json.dumps(
                {
                    "phase": "held",
                    "players": len(manager.media_focus.players),
                    "streams": list(manager.media_focus.streams),
                    "verified_muted": [
                        s["index"]
                        for s in held
                        if str(s.get("index")) in manager.media_focus.streams and s.get("mute")
                    ],
                }
            ),
            flush=True,
        )
        await asyncio.sleep(2)
    finally:
        await manager.release_media_focus()
    after = {str(s.get("index")): s for s in await manager._audio_listing("sink-inputs")}
    mismatches = [
        s["index"]
        for s in before
        if (now := after.get(str(s.get("index"))))
        and manager.media_focus._identity(s) == manager.media_focus._identity(now)
        and s.get("mute") != now.get("mute")
    ]
    print(
        json.dumps(
            {
                "phase": "restored",
                "mute_mismatches": mismatches,
                "pending": manager.media_focus.active,
            }
        ),
        flush=True,
    )
    if mismatches or manager.media_focus.active:
        raise RuntimeError("Media restoration requires attention")


asyncio.run(main())
