import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from ev.tools.holohand import exchange, status, set_paused
from ev.tools.base import ValidationError


class HoloHandControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "holohand.sock"
        self.commands = []
        self.state = "READY"
        self.oversize = False
        self.writers = []

        async def respond(reader, writer):
            self.writers.append(writer)
            cmd = (await reader.read(1024)).decode()
            self.commands.append(cmd)
            if cmd == "--pause":
                self.state = "PAUSED"
            if cmd == "--resume":
                self.state = "READY"
            reply = "x" * 8193 if self.oversize else self.state + " | input ready | idle\n"
            writer.write(reply.encode())
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        self.server = await asyncio.start_unix_server(respond, path=self.path)
        self.patch = patch("ev.tools.holohand.socket_path", return_value=self.path)
        self.patch.start()

    async def asyncTearDown(self):
        self.patch.stop()
        self.server.close()
        await self.server.wait_closed()
        for writer in self.writers:
            writer.close()
        self.temp.cleanup()

    async def test_pause_and_resume_have_fresh_status_readback(self):
        result = await set_paused({"paused": True}, None)
        self.assertTrue(result["verified"])
        self.assertEqual(self.commands, ["--pause", "--status"])
        result = await set_paused({"paused": False}, None)
        self.assertTrue(result["verified"])
        self.assertFalse(result["camera_started"])

    async def test_missing_socket_does_not_launch_anything(self):
        with patch("ev.tools.holohand.socket_path", return_value=self.path.with_name("missing")):
            self.assertFalse((await status({}, None))["available"])
        self.assertEqual(self.commands, [])

    async def test_unsafe_directory_rejected_before_sending(self):
        os.chmod(self.temp.name, 0o777)
        try:
            with self.assertRaises(ValidationError):
                await exchange("--resume")
        finally:
            os.chmod(self.temp.name, 0o700)
        self.assertEqual(self.commands, [])

    async def test_oversized_response_rejected(self):
        self.oversize = True
        with self.assertRaises(ValidationError):
            await exchange("--status")
