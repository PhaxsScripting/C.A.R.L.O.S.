import unittest
from unittest.mock import AsyncMock, patch
from ev.voice.echo import EchoCancel


class EchoBluetoothTests(unittest.IsolatedAsyncioTestCase):
    async def test_bluetooth_output_never_creates_echo_graph(self):
        echo = EchoCancel(True)
        echo._command = AsyncMock(return_value="bluez_output.test.1")
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
            self.assertEqual(await echo.ensure("alsa_input.test"), "alsa_input.test")
            spawn.assert_not_called()
        self.assertEqual(echo.state, "BYPASSED_BLUETOOTH")
        self.assertFalse(echo.active)

    async def test_switch_to_bluetooth_closes_existing_owned_filter(self):
        echo = EchoCancel(True)
        process = AsyncMock()
        process.returncode = None
        from unittest.mock import Mock

        process.terminate = Mock()
        echo.process = process
        echo.state = "ACTIVE"
        echo._command = AsyncMock(return_value="bluez_output.test.1")
        await echo.ensure("alsa_input.test")
        process.terminate.assert_called_once()
        process.wait.assert_awaited_once()
        self.assertIsNone(echo.process)
