import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
from ev.remote_health import configured_host, mobile_ready


class RemoteHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_preserves_host_restriction_without_contacting_configured_remote_address(
        self,
    ):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.json"
            config.write_text(json.dumps({"origin": "https://fixture.example"}))
            response = MagicMock(status=200)
            response.json = AsyncMock(return_value={"name": "Carlos Mobile", "online": True})
            session = MagicMock()
            session.get.return_value.__aenter__ = AsyncMock(return_value=response)
            with patch("ev.remote_health.aiohttp.ClientSession") as constructor:
                constructor.return_value.__aenter__ = AsyncMock(return_value=session)
                self.assertTrue(await mobile_ready(config))
            session.get.assert_called_once_with(
                "http://127.0.0.1:8765/api/health",
                headers={"Host": "fixture.example"},
                allow_redirects=False,
            )

    async def test_bad_configuration_reports_unavailable(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.json"
            for origin in [
                "file:///etc/passwd",
                "https://user:pass@fixture.example",
                "https://fixture.example/x",
                "https://fixture.example\r\nInjected: true",
            ]:
                config.write_text(json.dumps({"origin": origin}))
                self.assertFalse(await mobile_ready(config))
            self.assertFalse(await mobile_ready(config.parent / "missing"))
