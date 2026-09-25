from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..events import Event, PhaxEventBus
from .protocol import PROTOCOL_VERSION, ProtocolError, decode_message, encode_message

RequestHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

# These requests inspect state or interrupt work. Ordinary commands and tool
# executions stay ordered on each connection, while the UI can still stop
# speech or inspect progress during a long operation.
RESPONSIVE_REQUESTS = frozenset(
    {
        "carlos.status",
        "carlos.capabilities",
        "carlos.support",
        "carlos.privacy.set",
        "health",
        "snapshot",
        "panel.state",
        "voice.diagnostics",
        "events.history",
        "latency.report",
        "plan.list",
        "tool.catalog",
        "confirmation.list",
        "agent.tasks.list",
        "agent.tasks.get",
        "agent.tasks.steer",
        "tts.stop",
        "voice.privacy.set",
        "wake.pause.set",
        "plan.cancel",
        "core.stop",
    }
)


def _is_responsive_request(request: dict[str, Any]) -> bool:
    if request.get("type") in RESPONSIVE_REQUESTS:
        return True
    payload = request.get("payload", {})
    if request.get("type") == "command.submit" and isinstance(payload, dict):
        # Safety interrupts must not queue behind the command they must stop.
        # Exact stop grammar only: ordinary commands remain serialized.
        text = payload.get("text")
        if isinstance(text, str) and re.fullmatch(
            r"\s*(?:please\s+)?(?:stop|cancel|abort)\s+(?:everything|all(?:\s+(?:tasks|actions|commands))?)[.!?]*\s*",
            text,
            re.I,
        ):
            return True
    return (
        request.get("type") == "tool.call"
        and isinstance(payload, dict)
        and payload.get("name") == "desktop.input.disconnect"
    )


class IpcServer:
    def __init__(
        self,
        socket_path: Path,
        bus: PhaxEventBus,
        handler: RequestHandler,
        logger: logging.Logger,
        max_message_bytes: int = 1_048_576,
    ) -> None:
        self.socket_path = socket_path
        self.bus = bus
        self.handler = handler
        self.logger = logger
        self.max_message_bytes = max_message_bytes
        self.server: asyncio.AbstractServer | None = None
        self.clients = 0
        self._writers: set[asyncio.StreamWriter] = set()
        self._write_locks: dict[asyncio.StreamWriter, asyncio.Lock] = {}
        self._client_tasks: set[asyncio.Task[Any]] = set()

    async def start(self) -> None:
        if self.socket_path.exists():
            self.socket_path.unlink()
        # Bind and restrict synchronously before yielding or accepting clients.
        # start_unix_server(path=...) can publish the path before it returns,
        # leaving observers racing a later chmod and briefly wider permissions.
        bound = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            bound.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600)
            bound.setblocking(False)
            self.server = await asyncio.start_unix_server(
                self._client_connected,
                sock=bound,
                limit=self.max_message_bytes + 1,
            )
        except BaseException:
            bound.close()
            raise

    async def stop(self) -> None:
        server = self.server
        if server is not None:
            server.close()
        writers = tuple(self._writers)
        for writer in writers:
            writer.close()
        if writers:
            await asyncio.gather(
                *(writer.wait_closed() for writer in writers), return_exceptions=True
            )
        client_tasks = tuple(self._client_tasks)
        for task in client_tasks:
            task.cancel()
        if client_tasks:
            await asyncio.gather(*client_tasks, return_exceptions=True)
        if server is not None:
            await server.wait_closed()
            self.server = None
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass

    def _peer_is_current_user(self, writer: asyncio.StreamWriter) -> bool:
        from ..platform import peer_uid

        return peer_uid(writer.get_extra_info("socket")) == os.getuid()

    async def _write(self, writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
        lock = self._write_locks.setdefault(writer, asyncio.Lock())
        async with lock:
            writer.write(encode_message(message))
            await asyncio.wait_for(writer.drain(), timeout=5)

    async def _respond(self, writer: asyncio.StreamWriter, request: dict[str, Any]) -> None:
        try:
            result = await self.handler(request)
            message = {"type": "response", "id": request["id"], "payload": result}
        except Exception as error:
            self.logger.exception("IPC request failed", extra={"fields": {"error": str(error)}})
            message = {
                "type": "error",
                "id": request["id"],
                "payload": {"code": "request_failed", "message": str(error)[:500]},
            }
        await self._write(writer, message)

    async def _serve_requests(
        self,
        writer: asyncio.StreamWriter,
        queue: asyncio.Queue[dict[str, Any]],
    ) -> None:
        while True:
            request = await queue.get()
            try:
                await self._respond(writer, request)
            finally:
                queue.task_done()

    async def _send_events(self, writer: asyncio.StreamWriter, queue: asyncio.Queue[Event]) -> None:
        while True:
            event = await queue.get()
            try:
                await self._write(writer, {"type": "event", "payload": event.as_dict()})
            finally:
                queue.task_done()

    async def _client_connected(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if not self._peer_is_current_user(writer):
            writer.close()
            await writer.wait_closed()
            return
        self.clients += 1
        self._writers.add(writer)
        self._write_locks[writer] = asyncio.Lock()
        client_task = asyncio.current_task()
        assert client_task is not None
        self._client_tasks.add(client_task)
        subscriber_id: str | None = None
        event_task: asyncio.Task[None] | None = None
        request_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=32)
        request_task = asyncio.create_task(self._serve_requests(writer, request_queue))
        responsive_tasks: set[asyncio.Task[None]] = set()

        def response_done(task: asyncio.Task[None]) -> None:
            responsive_tasks.discard(task)
            if not task.cancelled() and task.exception() is not None:
                writer.close()

        # Wake the reader on a failed event/response writer, instead of keeping
        # a broken client subscribed indefinitely.
        def writer_done(task: asyncio.Task[None]) -> None:
            if not task.cancelled() and task.exception() is not None:
                writer.close()
                if task is request_task:
                    while not request_queue.empty():
                        request_queue.get_nowait()
                        request_queue.task_done()

        request_task.add_done_callback(writer_done)
        try:
            await self._write(
                writer,
                {
                    "type": "hello",
                    "payload": {
                        "protocol": PROTOCOL_VERSION,
                        "service": "Carlos",
                        "compatibility_service": "E.V.",
                        "pid": os.getpid(),
                    },
                },
            )
            while line := await reader.readline():
                try:
                    request = decode_message(line, self.max_message_bytes)
                    if request["type"] == "subscribe":
                        if subscriber_id is None:
                            subscriber_id, queue = self.bus.subscribe()
                            event_task = asyncio.create_task(self._send_events(writer, queue))
                            event_task.add_done_callback(writer_done)
                        result = {"subscribed": True, "sequence": self.bus.sequence}
                        await self._write(
                            writer, {"type": "response", "id": request["id"], "payload": result}
                        )
                    elif _is_responsive_request(request) and len(responsive_tasks) < 8:
                        task = asyncio.create_task(self._respond(writer, request))
                        responsive_tasks.add(task)
                        task.add_done_callback(response_done)
                    else:
                        try:
                            request_queue.put_nowait(request)
                        except asyncio.QueueFull:
                            await self._write(
                                writer,
                                {
                                    "type": "error",
                                    "id": request["id"],
                                    "payload": {
                                        "code": "busy",
                                        "message": "Too many queued requests; try again shortly.",
                                    },
                                },
                            )
                except ProtocolError as error:
                    await self._write(writer, {"type": "error", "payload": {"code": str(error)}})
                except Exception as error:
                    self.logger.exception(
                        "IPC request failed", extra={"fields": {"error": str(error)}}
                    )
                    await self._write(
                        writer,
                        {
                            "type": "error",
                            "payload": {"code": "request_failed", "message": str(error)[:500]},
                        },
                    )
            # Let an accepted operation finish if a client closes its socket.
            # Core shutdown explicitly cancels client tasks in stop().
            if not request_task.done():
                await request_queue.join()
            if responsive_tasks:
                await asyncio.gather(*responsive_tasks, return_exceptions=True)
        except (ConnectionError, BrokenPipeError, TimeoutError, ValueError):
            pass
        finally:
            if subscriber_id is not None:
                self.bus.unsubscribe(subscriber_id)
            tasks = [request_task, *responsive_tasks]
            if event_task is not None:
                tasks.append(event_task)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.clients -= 1
            self._client_tasks.discard(client_task)
            self._writers.discard(writer)
            self._write_locks.pop(writer, None)
            if not writer.is_closing():
                writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, BrokenPipeError):
                pass
