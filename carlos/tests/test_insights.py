import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from ev.insights import SystemInsights


class InsightTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.sequence = 0
        self.engine = SystemInsights(clock=lambda: self.now)

    def feed(self, payload, source="telemetry"):
        self.sequence += 1
        return self.engine.consume(
            SimpleNamespace(
                type="system.telemetry", source=source, sequence=self.sequence, payload=payload
            )
        )

    def test_sustained_pressure_not_transient_spikes(self):
        data = {"cpu_temperature": {"celsius": 95}}
        self.assertFalse(self.feed(data))
        self.now = 14
        self.assertFalse(self.feed(data))
        self.now = 15
        self.assertTrue(self.feed(data))
        item = self.engine.snapshot()[0]
        self.assertEqual(item["id"], "thermal")
        self.assertEqual(item["actions_executed"], 0)
        self.assertEqual(item["action"]["tool"], "system.get_temperature")
        self.now = 20
        self.assertFalse(self.feed(data))
        self.assertTrue(self.feed({"cpu_temperature": {"celsius": 80}}))
        self.assertEqual(self.engine.snapshot(), [])

    def test_dismissal_survives_repeated_condition_until_deadline(self):
        data = {"battery": {"percent": 10, "plugged": False}}
        self.feed(data)
        self.engine.dismiss("battery")
        self.now = 3599
        self.feed(data)
        self.assertEqual(self.engine.snapshot(), [])
        self.now = 3600
        self.assertTrue(self.feed(data))
        self.assertEqual(self.engine.snapshot()[0]["id"], "battery")

    def test_missing_unknown_and_untrusted_evidence_never_triggers(self):
        for data in (
            {},
            {"battery": {"percent": 5}},
            {"cpu_temperature": {"celsius": float("nan")}},
            {"disk": {"percent": 101}},
            {"memory": {"total_bytes": 0, "available_bytes": 0}},
        ):
            self.feed(data)
            self.now += 20
            self.feed(data)
            self.assertEqual(self.engine.snapshot(), [])
        self.feed({"battery": {"percent": 5, "plugged": False}}, source="web")
        self.assertEqual(self.engine.snapshot(), [])

    def test_bounded_cards_and_no_mutation_from_snapshot(self):
        data = {
            "cpu_temperature": {"celsius": 95},
            "disk": {"percent": 98},
            "memory": {"total_bytes": 1000, "available_bytes": 10},
            "battery": {"percent": 8, "plugged": False},
        }
        self.feed(data)
        self.now = 20
        self.feed(data)
        self.assertEqual(len(self.engine.snapshot()), 4)
        self.engine.snapshot()[0]["action"]["tool"] = "system.power"
        self.assertNotIn("system.power", [i["action"]["tool"] for i in self.engine.snapshot()])
        with self.assertRaises(ValueError):
            self.engine.dismiss("injected")

    def test_malformed_sensor_does_not_block_valid_observations(self):
        self.feed(
            {
                "cpu_temperature": None,
                "disk": "unavailable",
                "memory": [],
                "battery": {"percent": 5, "plugged": False},
            }
        )
        self.assertEqual([item["id"] for item in self.engine.snapshot()], ["battery"])
        self.assertTrue(self.feed({"battery": "disconnected"}))
        self.assertEqual(self.engine.snapshot(), [])

    def test_invalid_payload_clears_stale_evidence_without_crashing(self):
        for payload in (None, [], "unavailable"):
            self.feed({"battery": {"percent": 5, "plugged": False}})
            self.assertTrue(self.feed(payload))
            self.assertEqual(self.engine.snapshot(), [])


class InsightIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_event_worker_projects_cards_and_registry_reads_them_without_actions(self):
        import asyncio
        from ev.paths import Paths
        from ev.service import CarlosCore

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = CarlosCore(
                paths=Paths(*(root / n for n in ("config", "data", "state", "cache", "runtime")))
            )
            subscriber, queue = service.bus.subscribe()
            worker = asyncio.create_task(service._persist_events(queue))
            try:
                service.bus.publish(
                    "system.telemetry", "telemetry", {"battery": {"percent": 9, "plugged": False}}
                )
                await asyncio.wait_for(queue.join(), 2)
                self.assertEqual(service.snapshot()["insights"][0]["id"], "battery")
                result = await service.request_tool(
                    {"name": "agent.insights", "arguments": {}}, "inspect-fixture"
                )
                self.assertEqual(result["result"]["items"][0]["id"], "battery")
                self.assertFalse(
                    any(
                        e["type"] == "tool.started"
                        and e["payload"].get("tool") == "system.get_battery"
                        for e in service.bus.history()
                    )
                )
                await service.request_tool(
                    {"name": "agent.insights.dismiss", "arguments": {"id": "battery"}},
                    "dismiss-fixture",
                )
                self.assertEqual(service.snapshot()["insights"], [])
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
                service.bus.unsubscribe(subscriber)
                service.memory.close()
