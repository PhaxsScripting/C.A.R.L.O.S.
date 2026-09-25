import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from ev.health import GiggleGuard
from ev.events import PhaxEventBus


class HealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_failure_stops_after_three_repairs(self):
        h = GiggleGuard(SimpleNamespace(bus=PhaxEventBus()))
        healthy = AsyncMock(return_value=False)
        repair = AsyncMock()
        for now in (100, 131, 162, 193):
            await h.check("worker", healthy, repair, now)
        self.assertEqual(repair.await_count, 3)
        self.assertEqual(h.components["worker"]["state"], "BLOCKED")

    async def test_success_requires_observed_postcondition(self):
        h = GiggleGuard(SimpleNamespace(bus=PhaxEventBus()))
        repair = AsyncMock()
        await h.check("worker", AsyncMock(side_effect=[False, True]), repair, 100)
        self.assertTrue(h.components["worker"]["recovered"])

    async def test_healthy_worker_is_never_restarted(self):
        h = GiggleGuard(SimpleNamespace(bus=PhaxEventBus()))
        repair = AsyncMock()
        await h.check("worker", AsyncMock(return_value=True), repair, 100)
        repair.assert_not_called()

    async def test_hot_model_recovery_defers_without_spending_attempts(self):
        from ev.ai.local_llama import LocalLlamaProvider

        model = LocalLlamaProvider({"prewarm": True}, ownership_path="/unused-owner")
        model._managed_healthy = AsyncMock(return_value=False)
        model.prewarm = AsyncMock()
        h = GiggleGuard(SimpleNamespace(bus=PhaxEventBus(), brain=SimpleNamespace(provider=model)))
        with patch("ev.telemetry.read_temperature", return_value={"celsius": 90}):
            await h.check_local_model()
        self.assertEqual(h.components["Local AI"]["state"], "DEFERRED")
        self.assertEqual(len(h.attempts["Local AI"]), 0)
        model.prewarm.assert_not_called()

    async def test_cooled_model_recovery_verifies_health_after_warming(self):
        from ev.ai.local_llama import LocalLlamaProvider

        model = LocalLlamaProvider({"prewarm": True}, ownership_path="/unused-owner")
        model._managed_healthy = AsyncMock(side_effect=[False, False, True])
        model.prewarm = AsyncMock()
        h = GiggleGuard(SimpleNamespace(bus=PhaxEventBus(), brain=SimpleNamespace(provider=model)))
        with patch("ev.telemetry.read_temperature", return_value={"celsius": 65}):
            await h.check_local_model()
        model.prewarm.assert_awaited_once()
        self.assertTrue(h.components["Local AI"]["recovered"])

    async def test_external_model_is_not_warmed_or_probed(self):
        from ev.ai.local_llama import LocalLlamaProvider

        model = LocalLlamaProvider({"prewarm": True})
        model._managed_healthy = AsyncMock()
        model.prewarm = AsyncMock()
        h = GiggleGuard(SimpleNamespace(bus=PhaxEventBus(), brain=SimpleNamespace(provider=model)))
        await h.check_local_model()
        model._managed_healthy.assert_not_called()
        model.prewarm.assert_not_called()

    async def test_disabled_wake_is_not_restarted_by_supervisor(self):
        import asyncio

        voice = SimpleNamespace(
            wake_desired=False, privacy_mode=False, wake_paused=False, resource_suspended=False
        )
        core = SimpleNamespace(
            bus=PhaxEventBus(),
            voice=voice,
            state=SimpleNamespace(current=SimpleNamespace(value="DORMANT")),
        )
        health = GiggleGuard(core)
        health.check = AsyncMock()
        health.check_local_model = AsyncMock()
        with patch(
            "ev.health.asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])
        ):
            with self.assertRaises(asyncio.CancelledError):
                await health.run()
        health.check.assert_not_awaited()
        health.check_local_model.assert_awaited_once()
