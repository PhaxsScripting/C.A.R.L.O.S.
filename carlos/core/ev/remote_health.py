"""Probe only the loopback mobile backend, retaining its configured Host check."""

import json
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp


def configured_host(config_path):
    if config_path.stat().st_size > 65536:
        raise ValueError("Mobile configuration is too large")
    origin = json.loads(config_path.read_text())["origin"]
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or any(c in origin for c in "\r\n")
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Invalid configured mobile origin")
    return parsed.netloc


async def mobile_ready(config_path=None):
    config_path = config_path or Path.home() / ".local/state/holohand-remote/config.json"
    try:
        host = configured_host(config_path)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1.5)) as session:
            async with session.get(
                "http://127.0.0.1:8765/api/health", headers={"Host": host}, allow_redirects=False
            ) as response:
                data = await response.json()
                return (
                    response.status == 200
                    and isinstance(data, dict)
                    and data.get("online") is True
                    and data.get("name") == "Carlos Mobile"
                )
    except (OSError, ValueError, KeyError, TypeError, TimeoutError, aiohttp.ClientError):
        return False
