"""Bounded recovery of Carlos-owned workers, never arbitrary system services."""

import asyncio
import time
from collections import defaultdict, deque


class GiggleGuard:
    def __init__(self, core):
        self.core = core
        self.attempts = defaultdict(deque)
        self.components = {}

    async def check(self, name, healthy, repair, now=None):
        now = time.monotonic() if now is None else now
        attempts = self.attempts[name]
        while attempts and now - attempts[0] > 600:
            attempts.popleft()
        try:
            if await healthy():
                self.components[name] = {
                    "state": "READY",
                    "observed_at": time.time(),
                    "repairs_in_window": len(attempts),
                }
                return
            if len(attempts) >= 3:
                self.components[name] = {
                    "state": "BLOCKED",
                    "reason": "Three repair attempts in ten minutes; automatic retry paused",
                }
                return
            if attempts and now - attempts[-1] < 30:
                return
            attempts.append(now)
            self.components[name] = {"state": "RECOVERING", "attempt": len(attempts)}
            self.core.bus.publish(
                "health.repair_started", "health", {"component": name, "attempt": len(attempts)}
            )
            await asyncio.wait_for(repair(), 30)
            verified = await healthy()
            self.components[name] = {
                "state": "READY" if verified else "FAILED",
                "observed_at": time.time(),
                "recovered": verified,
            }
            self.core.bus.publish(
                "health.repair_finished", "health", {"component": name, "verified": verified}
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.components[name] = {"state": "FAILED", "error": str(error)[:200]}
            self.core.bus.publish(
                "health.repair_failed",
                "health",
                {"component": name, "error_type": type(error).__name__},
            )

    async def check_local_model(self):
        from .ai.carlos_router import CarlosRouter
        from .ai.local_llama import LocalHybridProvider, LocalLlamaProvider
        from .telemetry import read_temperature

        model = self.core.brain.provider
        if isinstance(model, CarlosRouter):
            model = model.local
        if isinstance(model, LocalHybridProvider):
            model = model.local
        # External endpoints and disabled/on-demand configurations are not
        # automatic recovery targets. Keep the adapter's ownership checks.
        if not isinstance(model, LocalLlamaProvider):
            return
        if model._ownership_path is None or not model.config.get("prewarm", True):
            return
        if await model._managed_healthy():
            self.components["Local AI"] = {"state": "READY", "observed_at": time.time()}
            return
        temperature = (await asyncio.to_thread(read_temperature)).get("celsius")
        ceiling = max(80.0, min(98.0, float(model.config.get("thermal_ceiling_celsius", 93))))
        # Leave headroom for model loading; waiting does not consume a repair
        # attempt. The adapter still enforces heat and RAM limits during work.
        if not isinstance(temperature, (int, float)) or temperature >= ceiling - 8:
            self.components["Local AI"] = {
                "state": "DEFERRED",
                "reason": "Waiting for verified thermal headroom",
                "temperature_celsius": temperature,
                "resume_below_celsius": ceiling - 8,
            }
            return
        await self.check("Local AI", model._managed_healthy, model.prewarm)

    async def run(self):
        # Give ordinary startup ownership and warmup time to settle.
        await asyncio.sleep(35)
        while True:
            c = self.core
            v = c.voice
            if (
                v.wake_desired
                and not v.privacy_mode
                and not v.wake_paused
                and not v.resource_suspended
                and c.state.current.value == "DORMANT"
            ):

                async def stt_ready():
                    # Adapter availability alone is not running-process evidence.
                    process = getattr(v.stt, "_server_process", None)
                    return process is not None and process.returncode is None

                # Only restart owned worker processes using their existing ownership checks.
                async def vad_ready():
                    return (
                        v.neural_vad.process is not None and v.neural_vad.process.returncode is None
                    )

                await self.check("VAD", vad_ready, v.neural_vad.start)
                if v.stt.config.get("persistent_server", False):
                    await self.check("STT", stt_ready, v.stt.prewarm)
                piper = getattr(v.tts, "piper", None)
                if piper is not None and piper.config.get("persistent_worker", False):

                    async def tts_ready():
                        return piper._worker is not None and piper._worker.returncode is None

                    await self.check("TTS", tts_ready, v.tts.prewarm)
            # Pausing wake listening is not a request to disable typed local AI.
            if not v.resource_suspended and c.state.current.value == "DORMANT":
                await self.check_local_model()
            await asyncio.sleep(15)


# Old integrations still know this name. Let them through.
HealthSupervisor = GiggleGuard
