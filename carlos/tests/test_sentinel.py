import importlib.util, tempfile, unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "sentinel_protocol", Path(__file__).resolve().parents[1] / "sentinel/protocol.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SentinelTests(unittest.TestCase):
    def test_replay_expiry_revocation_and_fixed_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = b"k" * 32
            devices = {"phone": {"key": key.hex(), "capabilities": ["wake", "status"]}}
            v = module.Verifier(Path(tmp) / "nonces.db", devices)
            e = module.sign("phone", "wake", key, now=100)
            self.assertEqual(v.verify(e, now=100), "wake")
            v.db.close()
            v = module.Verifier(Path(tmp) / "nonces.db", devices)
            with self.assertRaises(ValueError):
                v.verify(e, now=100)
            with self.assertRaises(ValueError):
                v.verify(module.sign("phone", "wake", key, now=90), now=200)
            with self.assertRaises(ValueError):
                v.verify(module.sign("phone", "shutdown", key, now=120), now=120)
            devices["phone"]["revoked"] = True
            with self.assertRaises(ValueError):
                v.verify(module.sign("phone", "status", key, now=120), now=120)
            v.db.close()
        self.assertEqual(len(module.magic_packet("01:02:03:04:05:06")), 102)
        with self.assertRaises(ValueError):
            module.magic_packet("host; arbitrary command")
