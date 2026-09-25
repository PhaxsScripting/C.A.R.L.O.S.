"""Bounded Spotify API integration using the user's existing local OAuth grant."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import stat
import tempfile
import time
from pathlib import Path
from typing import Any

import aiohttp


class SpotifyError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class SpotifyClient:
    def __init__(self, credentials: Path):
        self.credentials = credentials
        self._lock = asyncio.Lock()

    def status(self) -> dict[str, Any]:
        try:
            config = self._read_credentials()
        except SpotifyError as error:
            return {"configured": False, "message": str(error)}
        return {
            "configured": bool(config.get("refresh_token") or config.get("access_token")),
            "message": "Saved Spotify login found; API permissions and playback availability are checked when used.",
        }

    def _read_credentials(self) -> dict[str, Any]:
        try:
            info = self.credentials.lstat()
            if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise SpotifyError("Spotify credential file must be user-owned and mode 0600.")
            return json.loads(self.credentials.read_text())
        except (OSError, ValueError) as error:
            raise SpotifyError("Connect Spotify in your existing audio app first.") from error

    async def _token(self, session: aiohttp.ClientSession) -> str:
        config = await asyncio.to_thread(self._read_credentials)
        if config.get("access_token") and float(config.get("expires_at", 0)) > time.time() + 60:
            return str(config["access_token"])
        if not config.get("refresh_token") or not config.get("client_id"):
            raise SpotifyError("Spotify login needs reconnection in the audio app.")
        async with session.post(
            "https://accounts.spotify.com/api/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": config["refresh_token"],
                "client_id": config["client_id"],
            },
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise SpotifyError(
                    f"Spotify login refresh failed (HTTP {response.status}); reconnect in the audio app."
                )
            tokens = await response.json()
        config.update(
            access_token=tokens["access_token"],
            expires_at=time.time() + int(tokens.get("expires_in", 3600)),
        )
        if tokens.get("refresh_token"):
            config["refresh_token"] = tokens["refresh_token"]

        def persist():
            descriptor, path = tempfile.mkstemp(prefix=".ev-spotify-", dir=self.credentials.parent)
            try:
                with os.fdopen(descriptor, "w") as handle:
                    json.dump(config, handle)
                os.replace(path, self.credentials)
            finally:
                if os.path.exists(path):
                    os.unlink(path)

        await asyncio.to_thread(persist)
        return str(config["access_token"])

    async def _api(self, method: str, path: str, **kwargs) -> Any:
        if not path.startswith("/") or ":" in path:
            raise ValueError("Invalid Spotify API path")
        async with self._lock:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                token = await self._token(session)
                async with session.request(
                    method,
                    "https://api.spotify.com/v1" + path,
                    headers={"Authorization": "Bearer " + token},
                    allow_redirects=False,
                    **kwargs,
                ) as response:
                    if response.status == 204:
                        return {}
                    if response.status >= 300:
                        detail = {
                            401: "Reconnect Spotify in the audio app",
                            403: "Spotify's API rejected the saved connection. This is not an E.V. permission block. Check the Spotify app's allowed users, account eligibility and reconnect its login",
                            404: (
                                "No active Spotify device was found; open Spotify and start playback once"
                                if "/player" in path
                                else "Spotify could not find the requested item"
                            ),
                            429: "Spotify rate limit reached; try later",
                        }.get(response.status, "Spotify API request failed")
                        raise SpotifyError(f"{detail} (HTTP {response.status}).", response.status)
                    return await response.json()

    @staticmethod
    def _item(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": item.get("id"),
            "name": item.get("name"),
            "uri": item.get("uri"),
            "type": item.get("type"),
            "artists": [artist.get("name") for artist in item.get("artists", [])],
        }

    async def search(self, query: str, kind: str = "track") -> dict[str, Any]:
        if kind not in {"track", "album", "artist", "playlist"} or not query.strip():
            raise ValueError("Specify a track, album, artist or playlist search")
        data = await self._api("GET", "/search", params={"q": query, "type": kind, "limit": "5"})
        items = [self._item(item) for item in data.get(kind + "s", {}).get("items", []) if item]
        return {
            "items": items,
            "message": (
                "Spotify matches: "
                + "; ".join(
                    item["name"] + (" — " + ", ".join(item["artists"]) if item["artists"] else "")
                    for item in items
                )
                if items
                else "No Spotify matches found."
            ),
        }

    async def playlists(self) -> dict[str, Any]:
        data = await self._api("GET", "/me/playlists", params={"limit": "30"})
        items = [self._item(item) for item in data.get("items", []) if item]
        return {
            "items": items,
            "message": (
                "Your Spotify playlists: " + ", ".join(item["name"] for item in items)
                if items
                else "No playlists returned."
            ),
        }

    async def diagnose(self) -> dict[str, Any]:
        checks = []
        for path, label in (
            ("/me", "profile"),
            ("/me/playlists", "playlists"),
            ("/me/player/devices", "playback_devices"),
        ):
            try:
                data = await self._api("GET", path)
                checks.append(
                    {
                        "component": label,
                        "ok": True,
                        "count": len(data.get("items", data.get("devices", []))),
                    }
                )
            except SpotifyError as error:
                checks.append({"component": label, "ok": False, "http_status": error.status})
                if error.status in {401, 403, 429}:
                    break  # Do not hammer an account-wide rejection.
        available = all(row["ok"] for row in checks) and len(checks) == 3
        return {
            "available": available,
            "checks": checks,
            "ev_permission_block": False,
            "message": (
                "Spotify account reads are available; playback still requires an active eligible device."
                if available
                else "The saved Spotify API connection is not usable. Reconnect it in Phaxity Audio and check your Spotify developer app's user/account access. E.V. cannot fix this by granting itself PC permissions."
            ),
        }

    async def _own_playlist(self, query: str) -> dict[str, Any]:
        matches = []
        for offset in range(0, 500, 50):
            data = await self._api(
                "GET", "/me/playlists", params={"limit": "50", "offset": str(offset)}
            )
            matches.extend(
                self._item(item)
                for item in data.get("items", [])
                if item and str(item.get("name", "")).strip().casefold() == query.strip().casefold()
            )
            if not data.get("next"):
                if len(matches) != 1:
                    raise SpotifyError(
                        f"Found {len(matches)} exact saved playlists named {query!r}. Use an exact Spotify playlist URI; no music was started."
                    )
                return matches[0]
        raise SpotifyError(
            "Your playlist library exceeds the 500-playlist search limit. Give an exact playlist URI; no music was started."
        )

    async def play(
        self, query: str, kind: str = "track", *, own_playlist: bool = False, random: bool = False
    ) -> dict[str, Any]:
        if re.fullmatch(r"spotify:(track|album|artist|playlist):[A-Za-z0-9]{22}", query):
            uri = query
            selected = {"name": query, "uri": query}
        elif own_playlist and kind == "playlist":
            selected = await self._own_playlist(query)
            uri = str(selected["uri"])
        else:
            result = await self.search(query, kind)
            items = result["items"]
            normalized = query.casefold().strip()
            exact = [
                item
                for item in items
                if str(item["name"]).casefold() == normalized
                or (str(item["name"]) + " by " + ", ".join(item["artists"])).casefold()
                == normalized
            ]
            if len(exact) == 1:
                selected = exact[0]
            elif len(items) == 1:
                selected = items[0]
            else:
                return {
                    "verified": False,
                    **result,
                    "message": result["message"]
                    + " Specify the artist or an exact Spotify URI so I play the right one.",
                }
            uri = str(selected["uri"])
        if not re.fullmatch(r"spotify:(track|album|artist|playlist):[A-Za-z0-9]{22}", uri):
            raise SpotifyError("Spotify returned an unsupported playback URI")
        body = {"uris": [uri]} if uri.startswith("spotify:track:") else {"context_uri": uri}
        if random:
            if not uri.startswith("spotify:playlist:"):
                raise SpotifyError("Random selection requires one exact playlist")
            # Read the new playlist-items endpoint, retaining only playable
            # track positions. Bounded enumeration prevents unbounded scans.
            positions = []
            for offset in range(0, 1000, 50):
                data = await self._api(
                    "GET",
                    "/playlists/" + uri.rsplit(":", 1)[-1] + "/items",
                    params={"limit": "50", "offset": str(offset)},
                )
                for i, row in enumerate(data.get("items", [])):
                    track = row.get("item", row.get("track")) if isinstance(row, dict) else None
                    if (
                        track
                        and not track.get("is_local")
                        and track.get("is_playable") is not False
                        and str(track.get("uri", "")).startswith("spotify:track:")
                    ):
                        positions.append((offset + i, track["uri"]))
                if not data.get("next"):
                    break
            else:
                raise SpotifyError(
                    "Random selection is limited to playlists up to 1000 entries. No playback was started."
                )
            if not positions:
                raise SpotifyError("That playlist has no available tracks")
            position, selected_track = secrets.choice(positions)
            body["offset"] = {"position": position}
        await self._api("PUT", "/me/player/play", json=body)
        actual = await self._api("GET", "/me/player")
        active_uri = (
            (actual.get("item") or {}).get("uri")
            if uri.startswith("spotify:track:")
            else (actual.get("context") or {}).get("uri")
        )
        verified = bool(actual.get("is_playing") and active_uri == uri)
        if random:
            verified = verified and (actual.get("item") or {}).get("uri") == selected_track
        return {
            "verified": verified,
            "requested": True,
            "uri": uri,
            "message": (
                f"Playing {selected['name']}."
                if verified
                else "Spotify accepted the playback request, but the selected music has not been verified as playing yet."
            ),
        }
