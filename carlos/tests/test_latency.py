from __future__ import annotations

import unittest

from ev.events import PhaxEventBus


class LatencyReportTests(unittest.TestCase):
    def test_reports_measured_stages_and_null_for_missing_stages(self) -> None:
        bus = PhaxEventBus()
        correlation = "latency-test"
        bus.publish("wake.detected", "voice", {"latency_ms": 12.5}, correlation, 12.5)
        bus.publish(
            "wake.command_capture_started",
            "voice",
            {"wake_to_listening_ms": 7.25},
            correlation,
            7.25,
        )
        bus.publish("voice.transcription_complete", "voice", {}, correlation, 210.0)
        bus.publish("tool.completed", "tools", {"tool": "system.summary"}, correlation, 18.0)
        bus.publish("command.completed", "language", {}, correlation, 270.0)

        report = bus.latency_report(1)["recent"][0]["stages_ms"]
        self.assertEqual(report["wake_detection"], 12.5)
        self.assertEqual(report["wake_to_listening"], 7.25)
        self.assertEqual(report["speech_to_text"], 210.0)
        self.assertEqual(report["tool_execution_and_verification"], 18.0)
        self.assertEqual(report["command_total"], 270.0)
        self.assertIsNone(report["tts_startup"])

    def test_ignores_unrelated_housekeeping_correlations(self) -> None:
        bus = PhaxEventBus()
        bus.publish("system.telemetry", "telemetry", {"cpu": 1}, "housekeeping")
        self.assertEqual(bus.latency_report()["recent"], [])


if __name__ == "__main__":
    unittest.main()
