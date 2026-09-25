from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "core"))

from ev.events import PhaxEventBus  # noqa: E402
from ev.ipc.server import IpcServer, _is_responsive_request  # noqa: E402
from ev.ipc.protocol import encode_message  # noqa: E402
from ev.service import CarlosCore  # noqa: E402


class IpcResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[str] = []

        async def handler(request):
            self.calls.append(request["id"])
            if request["type"] == "tts.speak":
                self.started.set()
                await self.release.wait()
            if request["type"] == "tts.stop":
                self.release.set()
            if request["type"] == "invalid":
                raise ValueError("test failure")
            return {"ok": True}

        self.server = IpcServer(
            Path(self.directory.name) / "ev.sock",
            PhaxEventBus(),
            handler,
            logging.getLogger("ipc-test"),
        )
        await self.server.start()
        self.reader, self.writer = await asyncio.open_unix_connection(self.server.socket_path)
        self.assertEqual(json.loads(await self.reader.readline())["type"], "hello")

    async def asyncTearDown(self) -> None:
        self.release.set()
        self.writer.close()
        await self.writer.wait_closed()
        await self.server.stop()
        self.directory.cleanup()

    async def send(self, kind: str, request_id: str) -> None:
        self.writer.write(encode_message({"type": kind, "id": request_id, "payload": {}}))
        await self.writer.drain()

    async def receive(self) -> dict:
        return json.loads(await asyncio.wait_for(self.reader.readline(), 1))

    async def test_status_and_stop_work_while_same_connection_is_speaking(self) -> None:
        await self.send("tts.speak", "speech")
        await asyncio.wait_for(self.started.wait(), 1)
        await self.send("health", "health")
        self.assertEqual((await self.receive())["id"], "health")
        self.assertFalse(self.release.is_set())
        await self.send("tts.stop", "stop")
        replies = {item["id"] for item in [await self.receive(), await self.receive()]}
        self.assertEqual(replies, {"speech", "stop"})

    async def test_ordinary_mutations_remain_ordered(self) -> None:
        await self.send("tts.speak", "speech")
        await asyncio.wait_for(self.started.wait(), 1)
        await self.send("tool.call", "first")
        await self.send("tool.call", "second")
        await self.send("health", "health")
        self.assertEqual((await self.receive())["id"], "health")
        self.assertEqual(self.calls, ["speech", "health"])
        self.release.set()
        replies = [await self.receive() for _ in range(3)]
        self.assertEqual([item["id"] for item in replies], ["speech", "first", "second"])

    async def test_failure_retains_request_id_and_connection_remains_usable(self) -> None:
        with self.assertLogs("ipc-test", level="ERROR"):
            await self.send("invalid", "bad")
            reply = await self.receive()
        self.assertEqual(reply["type"], "error")
        self.assertEqual(reply["id"], "bad")
        await self.send("health", "good")
        self.assertEqual((await self.receive())["id"], "good")

    async def test_shutdown_cancels_inflight_work_and_reaps_clients(self) -> None:
        await self.send("tts.speak", "speech")
        await asyncio.wait_for(self.started.wait(), 1)
        await asyncio.wait_for(self.server.stop(), 1)
        self.assertEqual(self.server.clients, 0)
        self.assertFalse(self.server._client_tasks)
        self.assertFalse(self.server.socket_path.exists())

    def test_desktop_disconnect_uses_the_real_tool_call_request_name(self) -> None:
        self.assertTrue(
            _is_responsive_request(
                {
                    "type": "tool.call",
                    "payload": {"name": "desktop.input.disconnect", "arguments": {}},
                }
            )
        )
        self.assertFalse(
            _is_responsive_request(
                {
                    "type": "tool.call",
                    "payload": {"name": "desktop.pointer.click", "arguments": {}},
                }
            )
        )


class NotificationIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_queue_drop_accounting_allows_join(self) -> None:
        bus = PhaxEventBus(queue_size=1)
        _subscriber, queue = bus.subscribe()
        bus.publish("test.first", "test")
        bus.publish("test.second", "test")
        latest = await queue.get()
        self.assertEqual(latest.type, "test.second")
        queue.task_done()
        await asyncio.wait_for(queue.join(), 1)

    async def test_persistence_failure_is_isolated_from_later_telemetry(self) -> None:
        bus = PhaxEventBus()
        _subscriber, queue = bus.subscribe()
        calls = 0

        def record_event(_event):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("database unavailable")

        service = SimpleNamespace(
            _notification_queue=asyncio.Queue(maxsize=32),
            memory=SimpleNamespace(record_event=record_event),
            bus=bus,
            activity=SimpleNamespace(consume=Mock(return_value=False)),
            voice=SimpleNamespace(set_resource_mode=AsyncMock()),
            desktop=SimpleNamespace(
                input=SimpleNamespace(cancel_current=Mock(), close=AsyncMock())
            ),
            _background_tasks=set(),
            latest_telemetry={},
            logger=Mock(),
        )
        persistence = asyncio.create_task(CarlosCore._persist_events(service, queue))
        try:
            bus.publish("system.error", "test", {"message": "first"})
            for _ in range(100):
                if service.logger.exception.called:
                    break
                await asyncio.sleep(0.001)
            self.assertTrue(service.logger.exception.called)
            bus.publish("system.telemetry", "test", {"cpu": 7})
            await asyncio.wait_for(queue.join(), 1)
            self.assertEqual(service.latest_telemetry, {"cpu": 7})
            self.assertFalse(persistence.done())
        finally:
            persistence.cancel()
            await asyncio.gather(persistence, return_exceptions=True)

    async def test_slow_notifications_do_not_stall_persistence_or_telemetry(self) -> None:
        bus = PhaxEventBus()
        _subscriber, queue = bus.subscribe()
        notification_started = asyncio.Event()
        release_notification = asyncio.Event()
        recorded = asyncio.Event()
        loop = asyncio.get_running_loop()

        async def notify(_event):
            notification_started.set()
            await release_notification.wait()

        service = SimpleNamespace(
            _notification_queue=asyncio.Queue(maxsize=32),
            _notify_event=notify,
            bus=bus,
            activity=SimpleNamespace(consume=Mock(return_value=False)),
            memory=SimpleNamespace(
                record_event=lambda _event: loop.call_soon_threadsafe(recorded.set)
            ),
            voice=SimpleNamespace(set_resource_mode=AsyncMock()),
            latest_telemetry={},
            logger=Mock(),
        )
        persistence = asyncio.create_task(CarlosCore._persist_events(service, queue))
        notifications = asyncio.create_task(CarlosCore._deliver_notifications(service))
        try:
            bus.publish("system.error", "test", {"message": "test"})
            await asyncio.wait_for(notification_started.wait(), 1)
            await asyncio.wait_for(recorded.wait(), 1)
            bus.publish("system.telemetry", "test", {"cpu": 12})
            bus.publish("system.resource_mode_changed", "test", {"to": "PRESSURED"})
            for _ in range(100):
                if service.voice.set_resource_mode.await_count:
                    break
                await asyncio.sleep(0.001)
            self.assertEqual(service.latest_telemetry, {"cpu": 12})
            service.voice.set_resource_mode.assert_awaited_once_with("PRESSURED")
            self.assertFalse(release_notification.is_set())
        finally:
            persistence.cancel()
            notifications.cancel()
            await asyncio.gather(persistence, notifications, return_exceptions=True)

    async def test_notification_failure_does_not_kill_delivery_worker(self) -> None:
        service = SimpleNamespace(
            _notification_queue=asyncio.Queue(maxsize=32),
            _notify_event=AsyncMock(side_effect=[OSError("daemon unavailable"), None]),
            logger=Mock(),
        )
        bus = PhaxEventBus()
        for _ in range(2):
            service._notification_queue.put_nowait(bus.publish("system.error", "test"))
        task = asyncio.create_task(CarlosCore._deliver_notifications(service))
        try:
            await asyncio.wait_for(service._notification_queue.join(), 1)
            self.assertEqual(service._notify_event.await_count, 2)
            self.assertFalse(task.done())
            service.logger.warning.assert_called_once()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
