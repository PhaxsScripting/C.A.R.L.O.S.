from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "core"))

from ev.cli import request  # noqa: E402
from ev.ipc.protocol import encode_message  # noqa: E402


class FakeReader:
    def __init__(self) -> None:
        self.lines = iter(
            (
                encode_message({"type": "hello", "payload": {}}),
                encode_message(
                    {"type": "response", "id": "unused", "payload": {"blob": "x" * 100_000}}
                ),
                b"",
            )
        )

    async def readline(self) -> bytes:
        return next(self.lines)


class FakeWriter:
    def write(self, _data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


class CliTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_uses_ipc_sized_reader_limit(self) -> None:
        reader = FakeReader()
        writer = FakeWriter()
        with patch(
            "ev.cli.asyncio.open_unix_connection",
            new=AsyncMock(return_value=(reader, writer)),
        ) as connect:
            with patch("ev.cli.uuid.uuid4") as uuid4:
                uuid4.return_value.hex = "unused"
                response = await request("events.history", {"limit": 300})

        self.assertEqual(len(response["payload"]["blob"]), 100_000)
        self.assertEqual(connect.await_args.kwargs["limit"], 1_048_576)


if __name__ == "__main__":
    unittest.main()
