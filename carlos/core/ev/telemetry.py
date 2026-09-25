from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

import psutil

from .events import PhaxEventBus


def read_temperature() -> dict[str, Any]:
    from .platform import IS_FREEBSD

    if IS_FREEBSD:
        from .platform.system import temperature

        return temperature()
    candidates: list[tuple[str, float]] = []
    for hwmon in Path("/sys/class/hwmon").glob("hwmon*"):
        try:
            name = (hwmon / "name").read_text().strip()
        except OSError:
            continue
        for input_file in hwmon.glob("temp*_input"):
            try:
                value = float(input_file.read_text().strip()) / 1000.0
            except (OSError, ValueError):
                continue
            label_file = input_file.with_name(input_file.name.replace("_input", "_label"))
            try:
                label = label_file.read_text().strip()
            except OSError:
                label = input_file.stem
            candidates.append((f"{name}:{label}", value))
    preferred = [item for item in candidates if item[0].startswith("coretemp:Package")]
    if preferred:
        return {"celsius": round(preferred[0][1], 1), "sensor": preferred[0][0]}
    if candidates:
        hottest = max(candidates, key=lambda item: item[1])
        return {"celsius": round(hottest[1], 1), "sensor": hottest[0]}
    return {"celsius": None, "sensor": None}


class TelemetrySampler:
    def __init__(
        self, bus: PhaxEventBus, interval: float = 3.0, config: dict[str, Any] | None = None
    ) -> None:
        self.bus = bus
        self.interval = max(1.0, interval)
        self.config = config or {}
        self.process = psutil.Process(os.getpid())
        self._last_network = psutil.net_io_counters()
        self._last_network_time = time.monotonic()
        self._last_thermal_warning = 0.0
        self._resource_mode = "NORMAL"
        psutil.cpu_percent(interval=None)
        self.process.cpu_percent(interval=None)

    def sample(self) -> dict[str, Any]:
        now = time.monotonic()
        memory = psutil.virtual_memory()
        available_percent = memory.available / max(1, memory.total) * 100
        critical = float(self.config.get("critical_available_percent", 8.0))
        conservation = float(self.config.get("conservation_available_percent", 15.0))
        resource_mode = (
            "CRITICAL"
            if available_percent <= critical
            else "CONSERVATION" if available_percent <= conservation else "NORMAL"
        )
        swap = psutil.swap_memory()
        root = psutil.disk_usage("/")
        net = psutil.net_io_counters()
        elapsed = max(0.001, now - self._last_network_time)
        network = {
            "download_bytes_per_second": round(
                (net.bytes_recv - self._last_network.bytes_recv) / elapsed
            ),
            "upload_bytes_per_second": round(
                (net.bytes_sent - self._last_network.bytes_sent) / elapsed
            ),
            "connected_interfaces": sum(
                1 for stats in psutil.net_if_stats().values() if stats.isup
            ),
        }
        self._last_network = net
        self._last_network_time = now
        battery = psutil.sensors_battery()
        with self.process.oneshot():
            own = {
                "pid": self.process.pid,
                "rss_bytes": self.process.memory_info().rss,
                "cpu_percent": round(self.process.cpu_percent(interval=None), 2),
                "threads": self.process.num_threads(),
            }
        return {
            "cpu_percent": round(psutil.cpu_percent(interval=None), 1),
            "cpu_temperature": read_temperature(),
            "memory": {
                "used_bytes": memory.used,
                "available_bytes": memory.available,
                "total_bytes": memory.total,
                "percent": memory.percent,
            },
            "resource_mode": resource_mode,
            "swap": {"used_bytes": swap.used, "total_bytes": swap.total, "percent": swap.percent},
            "disk": {
                "used_bytes": root.used,
                "free_bytes": root.free,
                "total_bytes": root.total,
                "percent": root.percent,
            },
            "network": network,
            "battery": (
                None
                if battery is None
                else {
                    "percent": battery.percent,
                    "plugged": battery.power_plugged,
                    "seconds_left": battery.secsleft,
                }
            ),
            "uptime_seconds": round(time.time() - psutil.boot_time()),
            "ev_core": own,
        }

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            started = time.perf_counter()
            try:
                sample = await asyncio.to_thread(self.sample)
                self.bus.publish(
                    "system.telemetry",
                    "telemetry",
                    sample,
                    duration_ms=(time.perf_counter() - started) * 1000,
                )
                resource_mode = str(sample["resource_mode"])
                if resource_mode != self._resource_mode:
                    previous = self._resource_mode
                    self._resource_mode = resource_mode
                    self.bus.publish(
                        "system.resource_mode_changed",
                        "telemetry",
                        {
                            "from": previous,
                            "to": resource_mode,
                            "available_bytes": sample["memory"]["available_bytes"],
                        },
                    )
                temperature = sample["cpu_temperature"].get("celsius")
                threshold = float(self.config.get("warning_temperature_celsius", 90.0))
                repeat = float(self.config.get("warning_repeat_seconds", 120.0))
                now = time.monotonic()
                if (
                    temperature is not None
                    and temperature >= threshold
                    and now - self._last_thermal_warning >= repeat
                ):
                    self.bus.publish(
                        "system.warning",
                        "telemetry",
                        {
                            "kind": "thermal",
                            "message": f"CPU package temperature is {temperature:.0f} C",
                            "celsius": temperature,
                            "threshold": threshold,
                        },
                    )
                    self._last_thermal_warning = now
            except Exception as error:
                self.bus.publish("system.error", "telemetry", {"message": str(error)})
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.interval)
            except TimeoutError:
                pass
