import unittest
from unittest.mock import Mock
from ev.platform.freebsd_security import NativeSecurity


class FreeBSDSecurityTests(unittest.TestCase):
    def test_native_socket_scopes(self):
        runner = Mock(
            side_effect=[
                {
                    "stdout": "1000 ev 100 4 tcp4 127.0.0.1:8000 *:*\n0 sshd 20 3 tcp6 *:22 *:*\n",
                    "stderr": "",
                    "ok": True,
                },
                {
                    "stdout": "tcp4 0 0 192.0.2.1.1000 192.0.2.2.443 ESTABLISHED\n",
                    "stderr": "",
                    "ok": True,
                },
            ]
        )
        report = NativeSecurity(runner).sockets()
        self.assertEqual(report["localhost_only_count"], 1)
        self.assertEqual(report["network_accessible_count"], 1)
        self.assertEqual(report["established_connection_count"], 1)

    def test_unreadable_is_not_high_confidence(self):
        runner = Mock(return_value={"stdout": "", "stderr": "permission denied", "ok": False})
        report = NativeSecurity(runner).sockets()
        self.assertEqual(report["confidence"], "LOW")
        self.assertIsNone(report["established_connection_count"])

    def test_updates_does_not_scan_without_request(self):
        runner = Mock()
        self.assertEqual(NativeSecurity(runner).updates()["status"], "NOT_SCANNED")
        runner.assert_not_called()
