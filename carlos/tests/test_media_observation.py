import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from ev.tools.media import inspect_players
from ev.goals import validate_conditions, verify_conditions

SERVICE = "org.mpris.MediaPlayer2.fixture"


class MediaObservationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.focus = SimpleNamespace(
            lock=asyncio.Lock(),
            _run=AsyncMock(return_value=SERVICE),
            _owner=AsyncMock(return_value=":1.23"),
            _property=AsyncMock(return_value="Paused"),
        )
        self.context = SimpleNamespace(media_focus=self.focus)

    async def test_inspection_is_exact_readonly_and_omits_track_metadata(self):
        result = await inspect_players({"service": SERVICE}, self.context)
        self.assertFalse(result["partial"])
        self.assertEqual(result["players"][0]["owner"], ":1.23")
        self.assertFalse(result["changed"])
        self.focus._property.assert_awaited_once_with(":1.23", "PlaybackStatus")
        self.assertEqual(self.focus._owner.await_count, 2)
        self.assertNotIn("metadata", result["players"][0])

    async def test_owner_race_unknown_status_and_missing_service_do_not_fake_state(self):
        self.focus._owner.side_effect = [":1.23", ":1.24"]
        result = await inspect_players({}, self.context)
        self.assertTrue(result["partial"])
        self.assertEqual(result["players"], [])
        self.focus._owner.side_effect = None
        self.focus._property.return_value = "unknown"
        self.assertTrue((await inspect_players({}, self.context))["partial"])
        self.focus._owner.reset_mock()
        result = await inspect_players({"service": SERVICE + ".other"}, self.context)
        self.assertFalse(result["partial"])
        self.assertEqual(result["players"], [])
        self.focus._owner.assert_not_awaited()

    async def test_overloaded_player_list_is_bounded_and_explicitly_partial(self):
        self.focus._run.return_value = "\n".join(SERVICE + str(i) for i in range(20))
        result = await inspect_players({}, self.context)
        self.assertTrue(result["partial"])
        self.assertEqual(len(result["players"]), 16)

    async def test_invalid_service_cannot_become_command_argument(self):
        for name in ("spotify", ";sh", "org.mpris.MediaPlayer2.fixture\n--help"):
            with self.assertRaises(ValueError):
                await inspect_players({"service": name}, self.context)
        self.focus._run.assert_not_awaited()

    async def test_playback_goal_requires_same_owner_fresh_state_and_complete_observation(self):
        condition = {
            "kind": "media_playback",
            "service": SERVICE,
            "owner": ":1.23",
            "expected": "Paused",
        }
        row = {
            "service": SERVICE,
            "owner": ":1.23",
            "playback_status": "Paused",
            "captured_at_monotonic": time.monotonic(),
        }
        request = AsyncMock(
            return_value={"status": "completed", "result": {"players": [row], "partial": False}}
        )
        self.assertTrue((await verify_conditions([condition], request, "fixture"))["verified"])
        self.assertEqual(
            request.await_args.args[0], {"name": "audio.players", "arguments": {"service": SERVICE}}
        )
        for change in (
            {"owner": ":1.24"},
            {"service": SERVICE + ".other"},
            {"playback_status": "Playing"},
            {"captured_at_monotonic": time.monotonic() - 3},
            {"captured_at_monotonic": float("nan")},
        ):
            request.return_value["result"] = {"players": [{**row, **change}], "partial": False}
            self.assertFalse((await verify_conditions([condition], request, "fixture"))["verified"])
        for data in ({"players": []}, {"players": [row, row]}, {"players": [row], "partial": True}):
            request.return_value["result"] = data
            self.assertFalse((await verify_conditions([condition], request, "fixture"))["verified"])

    def test_bad_media_conditions_reject_before_observation(self):
        good = {
            "kind": "media_playback",
            "service": SERVICE,
            "owner": ":1.23",
            "expected": "Paused",
        }
        for change in (
            {"owner": "spotify"},
            {"expected": True},
            {"expected": {}},
            {"service": "spotify"},
        ):
            with self.assertRaises(ValueError):
                validate_conditions([{**good, **change}])
