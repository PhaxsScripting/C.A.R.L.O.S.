import os, socket, unittest
from unittest.mock import patch
from ev.platform import system
from ev.platform.freebsd_process import lifetime


class PlatformTests(unittest.TestCase):
    def test_linux_paths_preserved(self):
        with patch.object(system, "IS_FREEBSD", False):
            self.assertEqual(system.executable("/usr/bin/python3"), "/usr/bin/python3")

    def test_freebsd_python(self):
        import sys

        with patch.object(system, "IS_FREEBSD", True):
            self.assertEqual(system.executable("/usr/bin/python3"), sys.executable)

    def test_peer(self):
        a, b = socket.socketpair()
        try:
            self.assertEqual(system.peer_uid(a), os.getuid())
        finally:
            a.close()
            b.close()

    def test_no_peer(self):
        self.assertIsNone(system.peer_uid(None))

    def test_current_process_identity(self):
        p = system.process_identity(os.getpid())
        self.assertEqual(p["uid"], os.getuid())
        self.assertEqual(p["pid"], os.getpid())

    def test_lifetime_reuse(self):
        self.assertEqual(lifetime(os.getpid(), "1")["lifetime_status"], "REPLACED")

    def test_missing_temperature(self):
        with patch.object(system, "sysctl_text", return_value=None):
            self.assertIsNone(system.temperature()["celsius"])

    def test_temperature(self):
        with patch.object(system, "sysctl_text", return_value="51.2C"):
            self.assertEqual(system.temperature()["celsius"], 51.2)
