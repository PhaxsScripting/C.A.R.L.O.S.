from __future__ import annotations

import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "core"))

from ev.paths import Paths  # noqa: E402
from ev.security_center import SecurityCenter, _binding_scope  # noqa: E402


class SecurityCenterTests(unittest.TestCase):
    def test_binding_scope(self) -> None:
        self.assertEqual(_binding_scope("127.0.0.1:18080"), "LOCALHOST")
        self.assertEqual(_binding_scope("[::1]:22"), "LOCALHOST")
        self.assertEqual(_binding_scope("0.0.0.0:5353"), "ALL_INTERFACES")
        self.assertEqual(_binding_scope("192.168.1.4:8000"), "INTERFACE")

    def test_socket_parser_distinguishes_exposure(self) -> None:
        outputs = [
            {
                "ok": True,
                "returncode": 0,
                "stdout": 'tcp LISTEN 0 128 127.0.0.1:18080 0.0.0.0:* users:(("llama",pid=123,fd=4))\nudp UNCONN 0 0 0.0.0.0:5353 0.0.0.0:*\n',
                "stderr": "",
                "duration_ms": 1,
            },
            {
                "ok": True,
                "returncode": 0,
                "stdout": "tcp 0 0 10.0.0.2:4000 1.1.1.1:443\n",
                "stderr": "",
                "duration_ms": 1,
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            center = SecurityCenter(
                Paths(root / "config", root / "data", root / "state", root / "cache", root / "run"),
                {"security": {}},
            )
            with patch("ev.security_center._run", side_effect=outputs):
                result = center.sockets()
        self.assertEqual(result["listener_count"], 2)
        self.assertEqual(result["localhost_only_count"], 1)
        self.assertEqual(result["network_accessible_count"], 1)
        self.assertEqual(result["listeners"][0]["process"], "llama")

    def test_ev_sensitive_files_require_private_modes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = Paths(
                root / "config", root / "data", root / "state", root / "cache", root / "run"
            )
            paths.ensure()
            paths.config_file.write_text("{}")
            paths.database.write_text("db")
            server = socket.socket(socket.AF_UNIX)
            server.bind(str(paths.socket))
            try:
                os.chmod(paths.config_file, 0o600)
                os.chmod(paths.database, 0o600)
                os.chmod(paths.socket, 0o600)
                result = SecurityCenter(
                    paths, {"security": {"approval_mode": "codex_only"}}
                ).ev_security()
                self.assertEqual(result["findings"], [])
                self.assertTrue(result["ipc_local_only"])
                self.assertEqual(result["approval_scope"], ["development.coding_agent_execute"])
            finally:
                server.close()


if __name__ == "__main__":
    unittest.main()
