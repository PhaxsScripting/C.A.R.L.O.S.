import unittest
from ev.events import PhaxEventBus


class PriorityTests(unittest.TestCase):
    def test_priority_uses_observed_source_and_not_model_claim(self):
        bus = PhaxEventBus()
        self.assertEqual(
            bus.publish("ai.response", "language", {"priority": "EMERGENCY"}).priority, "NORMAL"
        )
        self.assertEqual(
            bus.publish("system.warning", "telemetry", {"kind": "thermal", "celsius": 99}).priority,
            "EMERGENCY",
        )
        self.assertEqual(
            bus.publish("system.warning", "telemetry", {"kind": "thermal", "celsius": 91}).priority,
            "HIGH",
        )
        self.assertEqual(
            bus.publish("coding.completed", "coding", {}).as_dict()["priority"], "BACKGROUND"
        )

    def test_telemetry_pressure_retains_emergency_and_task_order(self):
        bus = PhaxEventBus(queue_size=3)
        _, queue = bus.subscribe()
        warning = bus.publish("system.warning", "telemetry", {"kind": "thermal", "celsius": 99})
        started = bus.publish("task.started", "executor", {})
        bus.publish("voice.audio_level", "voice", {})
        completed = bus.publish("task.updated", "executor", {})
        for _ in range(20):
            bus.publish("system.telemetry", "telemetry", {})
        observed = [queue.get_nowait() for _ in range(3)]
        self.assertEqual(observed, [warning, started, completed])
        for _ in observed:
            queue.task_done()
        self.assertEqual(queue._unfinished_tasks, 0)

    def test_equal_priority_overflow_keeps_newest_events(self):
        bus = PhaxEventBus(queue_size=2)
        _, queue = bus.subscribe()
        for n in range(5):
            bus.publish("task.updated", "executor", {"n": n})
        self.assertEqual([queue.get_nowait().payload["n"] for _ in range(2)], [3, 4])
