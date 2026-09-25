#!/usr/bin/env python3
"""Native MPRIS check on an isolated D-Bus session; never controls real players."""

import asyncio
import json
import os
import sys
import threading
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from ev.tools.media import control_media, inspect_players
from ev.goals import verify_conditions
from ev.voice.media_focus import MediaFocus


class Player(dbus.service.Object):
    def __init__(self, name):
        self.test_bus = dbus.SessionBus(private=True)
        self.test_name = dbus.service.BusName(
            "org.mpris.MediaPlayer2." + name, self.test_bus, do_not_queue=True
        )
        super().__init__(self.test_name, "/org/mpris/MediaPlayer2")
        self.status = "Playing"
        self.track = 1

    @dbus.service.method("org.freedesktop.DBus.Properties", in_signature="ss", out_signature="v")
    def Get(self, interface, name):
        if interface != "org.mpris.MediaPlayer2.Player":
            raise dbus.exceptions.DBusException("Unknown interface")
        if name == "PlaybackStatus":
            return self.status
        if name == "Metadata":
            return dbus.Dictionary(
                {"mpris:trackid": dbus.ObjectPath(f"/track/{self.track}")}, signature="sv"
            )
        raise dbus.exceptions.DBusException("Unknown property")

    @dbus.service.method("org.mpris.MediaPlayer2.Player")
    def Play(self):
        self.status = "Playing"

    @dbus.service.method("org.mpris.MediaPlayer2.Player")
    def Pause(self):
        self.status = "Paused"

    @dbus.service.method("org.mpris.MediaPlayer2.Player")
    def Next(self):
        self.track += 1


async def main(spotify, firefox):
    async def listing(_kind):
        return []

    focus = MediaFocus(listing, os.environ.copy)
    context = SimpleNamespace(media_focus=focus)
    await focus.pause()
    assert spotify.status == firefox.status == "Paused"
    started = time.perf_counter()
    result = await control_media({"action": "pause", "player": "spotify"}, context)
    assert result["verified"], result
    print(
        json.dumps(
            {
                "native_pause_ms": round((time.perf_counter() - started) * 1000, 2),
                "message": result["message"],
            }
        )
    )
    await focus.restore()
    assert spotify.status == "Paused" and firefox.status == "Playing"
    started = time.perf_counter()
    result = await control_media({"action": "play", "player": "spotify"}, context)
    assert result["verified"] and spotify.status == "Playing", result
    print(
        json.dumps(
            {
                "native_resume_ms": round((time.perf_counter() - started) * 1000, 2),
                "message": result["message"],
            }
        )
    )
    await focus.pause()
    ambiguous = await control_media({"action": "pause"}, context)
    assert not ambiguous["verified"] and len(ambiguous["candidates"]) == 2, ambiguous
    await focus.restore()
    assert spotify.status == firefox.status == "Playing"
    print(
        json.dumps(
            {"isolated_bus": True, "unrelated_player_restored": True, "ambiguity_did_not_act": True}
        )
    )
    service = "org.mpris.MediaPlayer2.spotify"
    before = await inspect_players({"service": service}, context)
    assert not before["partial"] and len(before["players"]) == 1, before
    row = before["players"][0]

    async def observed(payload, correlation):
        assert payload["name"] == "audio.players"
        return {
            "status": "completed",
            "result": await inspect_players(payload["arguments"], context),
        }

    condition = {
        "kind": "media_playback",
        "service": service,
        "owner": row["owner"],
        "expected": "Playing",
    }
    assert (await verify_conditions([condition], observed, "isolated-media"))["verified"]
    assert not (
        await verify_conditions([{**condition, "expected": "Paused"}], observed, "isolated-media")
    )["verified"]
    # Exercise actual composed planning/validation/execution against these
    # native isolated-bus players, not mocked tool results or the user's core.
    from ev.paths import Paths
    from ev.service import CarlosCore

    with tempfile.TemporaryDirectory(prefix="ev-media-plan-") as directory:
        root = Path(directory)
        core = CarlosCore(
            paths=Paths(*(root / name for name in ("config", "data", "state", "cache", "runtime")))
        )
        core.tools.context.media_focus = focus
        planned_condition = {
            "kind": "media_playback",
            "service": {"$ref": "observe.result.players.0.service"},
            "owner": {"$ref": "observe.result.players.0.owner"},
            "expected": "Paused",
        }
        try:
            result = await core._request_model_tool(
                {
                    "name": "agent.execute_plan",
                    "arguments": {
                        "goal": "Pause the isolated fixture player and verify the same instance",
                        "steps": [
                            {
                                "id": "observe",
                                "tool": "audio.players",
                                "arguments": {"service": service},
                            },
                            {
                                "id": "pause",
                                "tool": "audio.media",
                                "arguments": {"action": "pause", "player": "spotify"},
                            },
                            {
                                "id": "wait",
                                "tool": "agent.wait_for",
                                "arguments": {
                                    "conditions": [planned_condition],
                                    "timeout_seconds": 2,
                                },
                            },
                        ],
                        "conditions": [planned_condition],
                    },
                },
                "isolated-media-plan",
            )
            assert result["status"] == "completed" and result["result"]["verified"], result
            assert spotify.status == "Paused" and firefox.status == "Playing"
            assert len(result["result"]["steps"]) == 3
            print(
                json.dumps(
                    {
                        "native_composed_media_plan": "PASSED",
                        "runtime_identity_references": True,
                        "same_instance_condition_verified": True,
                        "model_interpretation_tested": False,
                    }
                )
            )
        finally:
            core.memory.close()
    assert (await control_media({"action": "play", "player": "spotify"}, context))["verified"]
    # A replacement with the same public name/state must not satisfy an old
    # instance's completion condition. This is an entirely isolated session bus.
    spotify.test_bus.release_name(service)
    replacement = Player("spotify")
    players.append(replacement)  # Close only after the GLib dispatch thread stops.
    assert not (await verify_conditions([condition], observed, "isolated-media"))["verified"]
    fresh = await inspect_players({"service": service}, context)
    assert fresh["players"][0]["owner"] != row["owner"]
    assert spotify.status == firefox.status == replacement.status == "Playing"
    print(
        json.dumps(
            {
                "media_state_observation": "PASSED",
                "old_owner_rejected_after_replacement": True,
                "wrong_transport_state_rejected": True,
                "inspection_changed_playback": False,
                "actual_user_players_touched": False,
            }
        )
    )


if os.environ.get("EV_ISOLATED_MEDIA_TEST") != "1":
    raise SystemExit(
        "Use: EV_ISOLATED_MEDIA_TEST=1 dbus-run-session -- python3 scripts/check-media-transport.py"
    )
DBusGMainLoop(set_as_default=True)
loop = GLib.MainLoop()
players = [Player("spotify"), Player("firefox")]
thread_errors = []


def run_loop():
    try:
        loop.run()
    except BaseException as error:
        thread_errors.append(repr(error))


thread = threading.Thread(target=run_loop, daemon=True)
thread.start()
try:
    asyncio.run(main(*players))
finally:
    loop.quit()
    thread.join(timeout=2)
    assert not thread.is_alive(), "Fixture dispatch thread failed to stop"
    for player in players:
        player.remove_from_connection()
        player._name = None
        player.test_name = None
        player.test_bus.close()
    assert not thread_errors, thread_errors
