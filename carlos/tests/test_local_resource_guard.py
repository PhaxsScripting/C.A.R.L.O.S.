import asyncio
import os
import signal
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, PropertyMock, patch
from ev.ai.base import ProviderResourceError
from ev.ai.local_llama import LocalLlamaProvider


class LocalResourceGuardTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(
        hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"),
        "Linux PID descriptors required",
    )
    async def test_real_managed_child_is_reaped_when_cancelled_before_identity_completes(self):
        provider = LocalLlamaProvider({}, ownership_path="/unused-fixture-owner")
        provider._prepare_managed_endpoint = AsyncMock(return_value=False)
        provider._check_resources = AsyncMock()
        provider._server_command = lambda: [sys.executable, "-c", "import time; time.sleep(30)"]
        entered = asyncio.Event()

        async def identify(pid):
            entered.set()
            await asyncio.Future()

        provider._wait_for_server_identity = identify
        child = None
        with patch.object(
            LocalLlamaProvider, "available", new_callable=PropertyMock, return_value=(True, "")
        ):
            task = asyncio.create_task(provider._ensure_server())
            try:
                await asyncio.wait_for(entered.wait(), 2)
                child = provider._process
                self.assertIsNotNone(provider._pidfd)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 4)
                self.assertIsNotNone(child.returncode)
                self.assertIsNone(provider._process)
                self.assertIsNone(provider._pidfd)
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await provider.close()
                if child is not None and child.returncode is None:
                    child.terminate()
                    await child.wait()

    async def test_cancel_during_process_creation_obtains_then_reaps_only_returned_child(self):
        provider = LocalLlamaProvider({})
        provider._healthy = AsyncMock(return_value=False)
        provider._check_resources = AsyncMock()
        child = SimpleNamespace(
            pid=123, returncode=None, terminate=Mock(), wait=AsyncMock(return_value=0)
        )
        entered, release = asyncio.Event(), asyncio.Event()

        async def spawn(*args, **kwargs):
            entered.set()
            await release.wait()
            return child

        with patch.object(
            LocalLlamaProvider, "available", new_callable=PropertyMock, return_value=(True, "")
        ), patch("asyncio.create_subprocess_exec", side_effect=spawn):
            task = asyncio.create_task(provider._ensure_server())
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        child.terminate.assert_called_once()
        child.wait.assert_awaited_once()
        self.assertIsNone(provider._process)
        self.assertFalse(provider._owns_process)

    async def test_cancel_during_managed_identity_check_retains_cleanup(self):
        provider = LocalLlamaProvider({}, ownership_path="/unused-fixture-owner")
        provider._prepare_managed_endpoint = AsyncMock(return_value=False)
        provider._check_resources = AsyncMock()
        provider.close = AsyncMock()
        entered = asyncio.Event()

        async def identify(pid):
            entered.set()
            await asyncio.Future()

        provider._wait_for_server_identity = identify
        child = SimpleNamespace(pid=123, returncode=None)
        with patch.object(
            LocalLlamaProvider, "available", new_callable=PropertyMock, return_value=(True, "")
        ), patch("os.pidfd_open", side_effect=OSError), patch(
            "asyncio.create_subprocess_exec", AsyncMock(return_value=child)
        ):
            task = asyncio.create_task(provider._ensure_server())
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        provider.close.assert_awaited_once()

    async def test_loading_thermal_failure_closes_owned_child_before_any_request(self):
        provider = LocalLlamaProvider({})
        provider._healthy = AsyncMock(return_value=False)
        provider._check_resources = AsyncMock(
            side_effect=[None, ProviderResourceError("hot while loading")]
        )
        provider.close = AsyncMock()
        child = SimpleNamespace(pid=123, returncode=None)
        with patch.object(
            LocalLlamaProvider, "available", new_callable=PropertyMock, return_value=(True, "")
        ), patch("asyncio.create_subprocess_exec", AsyncMock(return_value=child)):
            with self.assertRaisesRegex(ProviderResourceError, "loading"):
                await provider._ensure_server()
        provider.close.assert_awaited_once()
        self.assertEqual(provider._check_resources.await_count, 2)

    async def test_cancellation_during_loading_closes_owned_child(self):
        provider = LocalLlamaProvider({})
        provider._healthy = AsyncMock(return_value=False)
        provider._check_resources = AsyncMock()
        provider.close = AsyncMock()
        loading = asyncio.Event()

        async def wait():
            loading.set()
            await asyncio.Future()

        provider._wait_until_ready = wait
        child = SimpleNamespace(pid=123, returncode=None)
        with patch.object(
            LocalLlamaProvider, "available", new_callable=PropertyMock, return_value=(True, "")
        ), patch("asyncio.create_subprocess_exec", AsyncMock(return_value=child)):
            task = asyncio.create_task(provider._ensure_server())
            await asyncio.wait_for(loading.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        provider.close.assert_awaited_once()

    async def test_hot_cpu_or_low_memory_fails_before_further_inference(self):
        provider = LocalLlamaProvider({})
        for temperature, available in [(95, 2**30), (70, 100 * 1048576)]:
            with patch(
                "ev.telemetry.read_temperature", return_value={"celsius": temperature}
            ), patch("psutil.virtual_memory", return_value=SimpleNamespace(available=available)):
                with self.assertRaises(ProviderResourceError):
                    await provider._resource_watch()

    async def test_resource_limit_cancels_request_and_closes_only_owned_runtime(self):
        provider = LocalLlamaProvider({})
        provider._owns_process = True
        provider._server_identity = {"pid": 123}
        provider._ensure_server = AsyncMock()
        provider._check_resources = AsyncMock()
        provider.close = AsyncMock()
        cancelled = asyncio.Event()

        async def generation(*args, **kwargs):
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        provider._post_unchecked = generation
        provider._resource_watch = AsyncMock(side_effect=ProviderResourceError("thermal"))
        with self.assertRaises(ProviderResourceError):
            await provider._post([], [])
        self.assertTrue(cancelled.is_set())
        provider.close.assert_awaited_once()

    async def test_success_or_cancellation_leaves_no_monitor_task(self):
        provider = LocalLlamaProvider({})
        provider._owns_process = True
        provider._server_identity = {"pid": 123}
        provider._ensure_server = AsyncMock()
        provider._check_resources = AsyncMock()
        stopped = asyncio.Event()

        async def watch():
            try:
                await asyncio.Future()
            finally:
                stopped.set()

        provider._resource_watch = watch
        provider._post_unchecked = AsyncMock(return_value={"result": "fixture"})
        self.assertEqual(await provider._post([], []), {"result": "fixture"})
        self.assertTrue(stopped.is_set())

    async def test_external_endpoint_is_never_signalled_or_monitored(self):
        provider = LocalLlamaProvider({})
        provider._ensure_server = AsyncMock()
        provider._post_unchecked = AsyncMock(return_value={"ok": True})
        provider._resource_watch = AsyncMock(side_effect=AssertionError("Not our process"))
        provider.close = AsyncMock()
        self.assertEqual(await provider._post([], []), {"ok": True})
        provider.close.assert_not_awaited()

    async def test_hot_preflight_never_starts_http_request(self):
        provider = LocalLlamaProvider({})
        provider._owns_process = True
        provider._server_identity = {"pid": 123}
        provider._ensure_server = AsyncMock()
        provider._check_resources = AsyncMock(side_effect=ProviderResourceError("hot"))
        provider._post_unchecked = AsyncMock()
        provider.close = AsyncMock()
        with self.assertRaises(ProviderResourceError):
            await provider._post([], [])
        provider._post_unchecked.assert_not_awaited()
        provider.close.assert_awaited_once()

    async def test_hot_system_never_spawns_or_restarts_model(self):
        provider = LocalLlamaProvider({})
        provider._healthy = AsyncMock(return_value=False)
        provider._check_resources = AsyncMock(side_effect=ProviderResourceError("hot"))
        with patch.object(
            LocalLlamaProvider, "available", new_callable=PropertyMock, return_value=(True, "")
        ), patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
            for _ in range(2):
                with self.assertRaises(ProviderResourceError):
                    await provider._ensure_server()
            spawn.assert_not_awaited()

    async def test_background_warmup_uses_guard_and_closes_owned_child(self):
        provider = LocalLlamaProvider({})
        provider._owns_process = True
        provider._server_identity = {"pid": 123}
        provider._ensure_server = AsyncMock()
        provider._check_resources = AsyncMock()
        provider._resource_watch = AsyncMock(side_effect=ProviderResourceError("hot"))
        provider.close = AsyncMock()
        cancelled = asyncio.Event()

        async def warm():
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        provider._warm_prompt = warm
        with self.assertRaises(ProviderResourceError):
            await provider.prewarm()
        self.assertTrue(cancelled.is_set())
        provider.close.assert_awaited_once()

    async def test_caller_cancellation_reaps_request_and_monitor(self):
        provider = LocalLlamaProvider({})
        provider._owns_process = True
        provider._server_identity = {"pid": 123}
        provider._ensure_server = AsyncMock()
        provider._check_resources = AsyncMock()
        started, cancelled, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def request(*args, **kwargs):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        async def watch():
            try:
                await asyncio.Future()
            finally:
                stopped.set()

        provider._post_unchecked, provider._resource_watch = request, watch
        task = asyncio.create_task(provider._post([], []))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())
        self.assertTrue(stopped.is_set())
