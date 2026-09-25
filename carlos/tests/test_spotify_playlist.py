import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from ev.spotify import SpotifyClient, SpotifyError
from ev.planner import TaskPlanner

PLAYLIST = "spotify:playlist:" + "a" * 22
TRACK = "spotify:track:" + "b" * 22


class SpotifyPlaylistTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SpotifyClient(Path("/missing-fixture"))

    async def test_random_track_uses_exact_saved_playlist_and_current_items_endpoint(self):
        calls = []

        async def api(method, path, **kwargs):
            calls.append((method, path, kwargs))
            if path == "/me/playlists":
                return {"items": [{"name": "Coding", "uri": PLAYLIST}], "next": None}
            if path.endswith("/items"):
                return {
                    "items": [{"item": None}, {"item": {"uri": TRACK, "is_playable": True}}],
                    "next": None,
                }
            if method == "PUT":
                self.assertEqual(
                    kwargs["json"], {"context_uri": PLAYLIST, "offset": {"position": 1}}
                )
                return {}
            return {"is_playing": True, "context": {"uri": PLAYLIST}, "item": {"uri": TRACK}}

        self.client._api = AsyncMock(side_effect=api)
        result = await self.client.play("coding", "playlist", own_playlist=True, random=True)
        self.assertTrue(result["verified"])
        self.assertFalse(any(path == "/search" for _, path, _ in calls))

    async def test_duplicate_playlist_names_or_blocked_connection_never_start_music(self):
        self.client._api = AsyncMock(
            return_value={"items": [{"name": "Coding", "uri": PLAYLIST}] * 2, "next": None}
        )
        with self.assertRaisesRegex(SpotifyError, "2 exact"):
            await self.client.play("Coding", "playlist", own_playlist=True)
        self.assertTrue(all(call.args[0] == "GET" for call in self.client._api.await_args_list))
        self.client._api = AsyncMock(side_effect=SpotifyError("blocked", 403))
        result = await self.client.diagnose()
        self.assertFalse(result["available"])
        self.assertFalse(result["ev_permission_block"])
        self.assertEqual(self.client._api.await_count, 1)

    async def test_playlist_pagination_checks_later_duplicate_before_playing(self):
        self.client._api = AsyncMock(
            side_effect=[
                {"items": [{"name": "Coding", "uri": PLAYLIST}], "next": "next"},
                {"items": [{"name": "Coding", "uri": PLAYLIST}], "next": None},
            ]
        )
        with self.assertRaises(SpotifyError):
            await self.client.play("Coding", "playlist", own_playlist=True)
        self.assertEqual(self.client._api.await_args_list[1].kwargs["params"]["offset"], "50")

    async def test_wrong_playing_track_is_not_verified_as_random_selection(self):
        self.client._api = AsyncMock(
            side_effect=[
                {"items": [{"name": "Coding", "uri": PLAYLIST}]},
                {"items": [{"item": {"uri": TRACK}}]},
                {},
                {
                    "is_playing": True,
                    "context": {"uri": PLAYLIST},
                    "item": {"uri": "spotify:track:" + "c" * 22},
                },
            ]
        )
        result = await self.client.play("Coding", "playlist", own_playlist=True, random=True)
        self.assertFalse(result["verified"])

    def test_spotify_403_is_external_access_not_permission_to_rewrite_agent(self):
        gap = TaskPlanner.__new__(TaskPlanner).capability_gap(
            "spotify.play", "Spotify rejected this (HTTP 403).", "MISSING_PERMISSION"
        )
        self.assertEqual(gap["type"], "EXTERNAL_ACCOUNT_ACCESS")
        self.assertFalse(gap["requires_user_approval"])
        self.assertTrue(gap["requires_external_account_action"])
