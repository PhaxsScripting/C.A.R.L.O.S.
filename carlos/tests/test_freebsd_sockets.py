import os
import subprocess
import unittest
from unittest.mock import patch
from ev.platform.freebsd_sockets import owns_listener


class NativeListenerTests(unittest.TestCase):
    def check(self, text, identities=None):
        identity = {"pid": 123, "uid": os.getuid(), "start_time": "1"}
        with patch(
            "ev.platform.freebsd_sockets.process_identity",
            side_effect=identities or [identity, identity],
        ), patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, text, "")):
            return owns_listener(123, "127.0.0.1", 18082)

    def test_exact_owner(self):
        self.assertTrue(self.check(f"{os.getuid()} whisper 123 4 tcp4 127.0.0.1:18082 *:*\n"))

    def test_other_pid(self):
        self.assertFalse(self.check(f"{os.getuid()} whisper 124 4 tcp4 127.0.0.1:18082 *:*\n"))

    def test_wildcard(self):
        self.assertFalse(self.check(f"{os.getuid()} whisper 123 4 tcp4 *:18082 *:*\n"))

    def test_pid_reuse(self):
        self.assertFalse(
            self.check(
                f"{os.getuid()} whisper 123 4 tcp4 127.0.0.1:18082 *:*\n",
                [{"uid": os.getuid(), "start_time": "1"}, {"uid": os.getuid(), "start_time": "2"}],
            )
        )

    def test_empty_or_malformed(self):
        self.assertFalse(self.check(""))
        self.assertFalse(self.check("unparseable"))
