import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from ev.service import CarlosCore


class PetPanelTests(unittest.IsolatedAsyncioTestCase):
    async def test_panel_reports_privacy_without_conversation_or_window_content(self):
        for mode, muted, expected in [
            ("NORMAL", False, False),
            ("NORMAL", True, True),
            ("PRIVATE SESSION", False, True),
            ("GUEST", False, True),
        ]:
            core = CarlosCore.__new__(CarlosCore)
            core.voice = SimpleNamespace(snapshot=Mock(return_value={"privacy_mode": muted}))
            core.privacy = SimpleNamespace(mode=mode)
            core.state = SimpleNamespace(current=SimpleNamespace(value="DORMANT"), detail="idle")
            result = await core.handle_request({"type": "panel.state", "payload": {}})
            if mode == "GUEST":
                self.assertEqual(result["status"], "denied")
            else:
                self.assertEqual(result["privacy_mode"], expected)
            self.assertNotIn("conversation", result)
            self.assertNotIn("windows", result)
