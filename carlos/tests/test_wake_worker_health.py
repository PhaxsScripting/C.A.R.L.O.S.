import asyncio
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from ev.events import PhaxEventBus
from ev.state import StateMachine
from ev.voice.manager import VoiceManager
from ev.voice.wake import WakeWordWorker
from ev.voice import wake_worker
from ev.service import CarlosCore


class WakeWorkerHealthTests(unittest.IsolatedAsyncioTestCase):
    def test_silent_input_does_not_trigger_watchdog(self):
        worker = WakeWordWorker({})
        worker.fed_bytes = 16000
        with patch("ev.voice.wake.time.monotonic", return_value=5000):
            worker.check_progress()
        self.assertEqual(worker.backlog_since, 0)

    def test_sustained_audio_backlog_triggers_restart(self):
        worker = WakeWordWorker({})
        worker.fed_bytes = 128000
        with patch("ev.voice.wake.time.monotonic", return_value=10):
            worker.check_progress()
        with patch("ev.voice.wake.time.monotonic", return_value=14.1):
            with self.assertRaisesRegex(RuntimeError, "stopped consuming"):
                worker.check_progress()

    def test_progress_clears_watchdog_before_new_stall(self):
        worker = WakeWordWorker({})
        worker.fed_bytes = 128000
        worker.backlog_since = 1
        worker.processed_bytes = 96000
        with patch("ev.voice.wake.time.monotonic", return_value=50):
            worker.check_progress()
        self.assertEqual(worker.backlog_since, 0)

    def test_detection_callback_does_not_look_like_stalled_decode(self):
        worker = WakeWordWorker({})
        worker.fed_bytes = 128000
        worker.backlog_since = 1
        worker.callback_active = True
        worker.check_progress()
        self.assertEqual(worker.backlog_since, 0)

    async def test_progress_protocol_is_monotonic_and_bounded(self):
        worker = WakeWordWorker({})
        worker.fed_bytes = 96000
        output = asyncio.StreamReader()
        for value in (64000, 32000, -1, 999999, "bad", True):
            output.feed_data(
                (json.dumps({"type": "progress", "processed_bytes": value}) + "\n").encode()
            )
        output.feed_eof()
        worker.process = SimpleNamespace(stdout=output, returncode=None)
        await worker._read_events()
        self.assertEqual(worker.processed_bytes, 64000)
        self.assertEqual(worker.health["backlog_ms"], 1000)
        self.assertIsNotNone(worker.health["last_progress_age_ms"])

    async def test_callback_failure_does_not_lose_progress_reader(self):
        worker = WakeWordWorker({})
        worker.fed_bytes = 32000
        worker.on_detection = AsyncMock(side_effect=ValueError("test race"))
        output = asyncio.StreamReader()
        output.feed_data(
            b'{"type":"detected","keyword":"EVIE"}\n{"type":"progress","processed_bytes":32000}\n'
        )
        output.feed_eof()
        worker.process = SimpleNamespace(stdout=output, returncode=None)
        await worker._read_events()
        self.assertFalse(worker.callback_active)
        self.assertEqual(worker.processed_bytes, 32000)
        self.assertIn("test race", worker.last_error)

    def test_worker_emits_progress_for_consumed_audio(self):
        spotter = MagicMock()
        spotter.is_ready.return_value = False
        library = SimpleNamespace(KeywordSpotter=MagicMock(return_value=spotter))
        argv = [
            "wake_worker",
            "--tokens",
            "tokens",
            "--encoder",
            "enc",
            "--decoder",
            "dec",
            "--joiner",
            "join",
            "--keywords",
            "keywords",
        ]
        output = io.StringIO()
        with patch.dict(sys.modules, {"sherpa_onnx": library}), patch.object(
            sys, "argv", argv
        ), patch.object(
            sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"\0" * 64000))
        ), redirect_stdout(
            output
        ):
            self.assertEqual(wake_worker.main(), 0)
        messages = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(
            [message["processed_bytes"] for message in messages if message["type"] == "progress"],
            [32000, 64000],
        )

    def test_live_status_requires_recent_microphone_samples(self):
        bus = PhaxEventBus()
        manager = VoiceManager({"capture_command": ["/missing"]}, bus, StateMachine(bus))
        manager.wake.process = SimpleNamespace(returncode=None)
        manager.wake.reader_task = SimpleNamespace(done=lambda: False)
        manager.wake_audio_process = SimpleNamespace(returncode=None)
        with patch("ev.voice.manager.time.monotonic", return_value=100):
            manager.wake_last_feed_at = 90
            self.assertFalse(manager.snapshot()["wake_active"])
            self.assertFalse(manager.snapshot()["microphone_active"])
            manager.wake_last_feed_at = 99
            self.assertTrue(manager.snapshot()["wake_active"])
            manager.wake.reader_task = None
            self.assertFalse(manager.snapshot()["wake_active"])
            manager.wake.reader_task = SimpleNamespace(done=lambda: True)
            self.assertFalse(manager.snapshot()["wake_active"])
            manager.wake.reader_task = SimpleNamespace(done=lambda: False)
            manager.wake_audio_process.returncode = 1
            self.assertFalse(manager.snapshot()["wake_active"])

    def test_diagnostics_do_not_equate_installed_model_with_live_listener(self):
        service = CarlosCore.__new__(CarlosCore)
        voice = {
            "wake_available": True,
            "wake_enabled": True,
            "wake_active": False,
            "diagnostics": {"wake_state": "RECONNECTING"},
        }
        service.voice = SimpleNamespace(snapshot=lambda: voice)
        service.brain = SimpleNamespace(provider_status=lambda: {})
        service.accessibility = SimpleNamespace(status=lambda: {"status": "READY"})
        service.vision = SimpleNamespace(status=lambda: {})
        service.desktop = SimpleNamespace(
            input=SimpleNamespace(status=lambda: {"available": True, "connected": True})
        )
        service.coding_agent = SimpleNamespace(status=lambda: {"available": True})
        service.kwin_bridge = SimpleNamespace(status={"available": True, "reason": "fixture"})
        service.paths = SimpleNamespace(socket="test-only")
        service.tools = SimpleNamespace(catalog=lambda: [])

        def status():
            return next(
                row["status"]
                for row in service.self_diagnostics()["checks"]
                if row["component"] == "wake_word"
            )

        self.assertEqual(status(), "DEGRADED")
        voice["wake_active"] = True
        self.assertEqual(status(), "PASS")
        voice["wake_paused"] = True
        self.assertEqual(status(), "PAUSED")
        voice["wake_paused"] = False
        voice["wake_enabled"] = False
        self.assertEqual(status(), "DISABLED")
