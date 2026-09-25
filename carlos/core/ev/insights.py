"""Local telemetry-driven assistance, never autonomous permission escalation.

Uses existing observations only. No extra polling, model requests, desktop
notifications, microphone reads, process killing or configuration changes.
"""

import copy
import math
import time


class SystemInsights:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.items = {}
        self.dismissed_until = {}
        self.condition_since = {}
        self.last_sequence = 0

    @staticmethod
    def number(value):
        return float(value) if type(value) in (int, float) and math.isfinite(value) else None

    def consume(self, event):
        if (
            event.type != "system.telemetry"
            or event.source != "telemetry"
            or event.sequence <= self.last_sequence
        ):
            return False
        self.last_sequence = event.sequence
        before = self.snapshot()
        now = self.clock()
        data = event.payload if isinstance(event.payload, dict) else {}

        # A sensor/backend failure is unknown evidence, not a stale warning or
        # an exception that prevents processing the other valid sensors.
        def sensor(name):
            value = data.get(name)
            return value if isinstance(value, dict) else {}

        temperature = self.number(sensor("cpu_temperature").get("celsius"))
        disk = self.number(sensor("disk").get("percent"))
        total = self.number(sensor("memory").get("total_bytes"))
        available = self.number(sensor("memory").get("available_bytes"))
        memory_low = (
            total is not None
            and total > 0
            and available is not None
            and 0 <= available / total < 0.08
        )
        battery = sensor("battery")
        charge = self.number(battery.get("percent"))
        conditions = [
            (
                "thermal",
                temperature is not None and temperature >= 90,
                15,
                "CPU temperature remains high",
                "Inspect temperature and heavy processes before changing power settings.",
                {"tool": "system.get_temperature", "arguments": {}},
            ),
            (
                "memory",
                memory_low,
                15,
                "Available RAM remains below eight percent",
                "Inspect memory-heavy processes. Unsaved applications will not be closed automatically.",
                {"tool": "system.get_processes", "arguments": {"sort": "memory", "limit": 12}},
            ),
            (
                "storage",
                disk is not None and 95 <= disk <= 100,
                15,
                "Root filesystem is nearly full",
                "Inspect disk usage before selecting anything to clean up. Nothing will be deleted automatically.",
                {"tool": "system.get_disk_usage", "arguments": {"path": "/"}},
            ),
            (
                "battery",
                charge is not None and 0 <= charge <= 15 and battery.get("plugged") is False,
                0,
                "Battery is low and unplugged",
                "Connect power or inspect the battery status. Power settings have not been changed.",
                {"tool": "system.get_battery", "arguments": {}},
            ),
        ]
        for key, active, dwell, title, detail, action in conditions:
            if not active:
                self.condition_since.pop(key, None)
                self.items.pop(key, None)
                continue
            since = self.condition_since.setdefault(key, now)
            if now - since >= dwell and now >= self.dismissed_until.get(key, 0):
                self.items[key] = {
                    "id": key,
                    "title": title,
                    "detail": detail,
                    "action": action,
                    "source": "local system telemetry",
                    "observation_sequence": event.sequence,
                    "first_observed_monotonic": since,
                    "updated_monotonic": now,
                    "actions_executed": 0,
                    "requires_explicit_selection": True,
                }
            elif now < self.dismissed_until.get(key, 0):
                self.items.pop(key, None)
        # Sequence/timestamps are fresh evidence but need not redraw unchanged
        # advice every three seconds. State changes still publish immediately.
        return [(i["id"], i["title"]) for i in before] != [
            (i["id"], i["title"]) for i in self.snapshot()
        ]

    def snapshot(self):
        return copy.deepcopy(list(self.items.values()))

    def dismiss(self, identifier):
        if identifier not in {"thermal", "memory", "storage", "battery"}:
            raise ValueError("Unknown insight")
        self.dismissed_until[identifier] = self.clock() + 3600
        self.items.pop(identifier, None)
        return {"dismissed": identifier, "snoozed_seconds": 3600, "actions_executed": 0}
