import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from ev.commands import direct_action
from ev.tools.startup import startup_list, boot_status


class StartupInspectionTests(unittest.TestCase):
    def test_boot_queries_are_read_only_and_how_to_questions_are_not_actions(self):
        self.assertEqual(direct_action("check my boot status").tool, "system.boot.status")
        self.assertEqual(direct_action("show startup applications").tool, "system.startup.list")
        self.assertIsNone(direct_action("How do I change boot settings?"))
        self.assertFalse(boot_status({}, None)["bootloader_modified"])

    def test_user_hidden_entry_overrides_system_autostart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system = root / "system" / "autostart"
            user = root / "user" / "autostart"
            system.mkdir(parents=True)
            user.mkdir(parents=True)
            (system / "example.desktop").write_text(
                "[Desktop Entry]\nName=Example\nExec=/bin/false\n"
            )
            (user / "example.desktop").write_text(
                "[Desktop Entry]\nName=Disabled Example\nHidden=true\n"
            )
            before = (user / "example.desktop").read_bytes()
            with patch.dict(
                os.environ,
                {"XDG_CONFIG_DIRS": str(root / "system"), "XDG_CONFIG_HOME": str(root / "user")},
            ):
                result = startup_list({}, None)
            self.assertEqual(len(result["applications"]), 1)
            self.assertFalse(result["applications"][0]["enabled"])
            self.assertEqual(result["applications"][0]["scope"], "user")
            self.assertEqual(before, (user / "example.desktop").read_bytes())
