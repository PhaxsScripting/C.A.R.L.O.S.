import asyncio
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from ev.events import PhaxEventBus
from ev.ipc.server import IpcServer


class IpcStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_permissions_restricted_before_first_async_accept_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "core.sock"
            server = IpcServer(
                path, PhaxEventBus(), AsyncMock(), logging.getLogger("test-ipc-startup")
            )
            real_start = asyncio.start_unix_server

            async def inspected(callback, **kwargs):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertIn("sock", kwargs)
                self.assertNotIn("path", kwargs)
                return await real_start(callback, **kwargs)

            with patch("ev.ipc.server.asyncio.start_unix_server", side_effect=inspected):
                await server.start()
            await server.stop()
