from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .logging_utils import redact
from .identity import event_name

TRANSIENT_EVENT_TYPES = frozenset({"voice.audio_level", "tts.audio_level"})


@dataclass(slots=True)
class Event:
    sequence: int
    type: str
    source: str
    payload: dict[str, Any]
    correlation_id: str
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    monotonic: float = field(default_factory=time.monotonic)
    duration_ms: float | None = None
    private: bool = False

    @property
    def priority(self):
        from .priority import priority_for

        return priority_for(self.type, self.source, self.payload)

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "protocol_version": 1,
            "priority": self.priority,
            "name": event_name(self.type, self.payload),
            "sequence": self.sequence,
            "type": self.type,
            "source": self.source,
            "payload": redact(self.payload),
            "correlation_id": self.correlation_id,
            "timestamp": self.timestamp,
        }
        if self.duration_ms is not None:
            result["duration_ms"] = round(self.duration_ms, 3)
        return result


class PhaxEventBus:
    def __init__(self, history_limit: int = 300, queue_size: int = 512) -> None:
        self._sequence = 0
        self._history: deque[Event] = deque(maxlen=history_limit)
        self._subscribers: dict[str, asyncio.Queue[Event]] = {}
        self._queue_size = queue_size
        self.private = False

    def publish(
        self,
        event_type: str,
        source: str,
        payload: dict[str, Any] | None = None,
        correlation_id: str | None = None,
        duration_ms: float | None = None,
    ) -> Event:
        self._sequence += 1
        event = Event(
            sequence=self._sequence,
            type=event_type,
            source=source,
            payload=payload or {},
            correlation_id=correlation_id or uuid.uuid4().hex,
            duration_ms=duration_ms,
            private=self.private,
        )
        # Waveform samples are live UI telemetry (20-25 Hz), not audit events.
        # Retaining them would evict the useful 300-entry task history within
        # seconds even though the UI already consumes them via subscription.
        if event_type not in TRANSIENT_EVENT_TYPES:
            self._history.append(event)
        for queue in tuple(self._subscribers.values()):
            if queue.full():
                # Retain critical events under telemetry pressure without
                # reordering a task's start/result sequence. This runs without
                # yielding, so subscribers cannot race the bounded refill.
                pending = [queue.get_nowait() for _ in range(queue.qsize())]
                ranks = {"BACKGROUND": 0, "NORMAL": 1, "HIGH": 2, "EMERGENCY": 3}
                victim = min(range(len(pending)), key=lambda i: ranks[pending[i].priority])
                if ranks[event.priority] >= ranks[pending[victim].priority]:
                    pending.pop(victim)
                    pending.append(event)
                for retained in pending:
                    queue.put_nowait(retained)
                # Refill before balancing removed entries: Queue.join must not
                # observe a transient empty unfinished-task count.
                for _ in pending:
                    queue.task_done()
            else:
                queue.put_nowait(event)
        return event

    def subscribe(self) -> tuple[str, asyncio.Queue[Event]]:
        subscriber_id = uuid.uuid4().hex
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers[subscriber_id] = queue
        return subscriber_id, queue

    def unsubscribe(self, subscriber_id: str) -> None:
        self._subscribers.pop(subscriber_id, None)

    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        bounded = max(1, min(limit, len(self._history)))
        return [event.as_dict() for event in list(self._history)[-bounded:]]

    def latency_report(self, limit: int = 12) -> dict[str, Any]:
        """Return measured pipeline timings without inventing missing stages."""

        groups: dict[str, list[Event]] = {}
        for event in self._history:
            if event.correlation_id:
                groups.setdefault(event.correlation_id, []).append(event)
        reports: list[dict[str, Any]] = []
        for correlation_id, events in groups.items():
            types = {event.type for event in events}
            if not types.intersection(
                {
                    "wake.detected",
                    "command.received",
                    "plan.created",
                    "tool.started",
                    "voice.transcription_started",
                    "tts.started",
                }
            ):
                continue

            def first(event_type: str) -> Event | None:
                return next((item for item in events if item.type == event_type), None)

            def duration(event_type: str) -> float | None:
                event = first(event_type)
                return (
                    None
                    if event is None or event.duration_ms is None
                    else round(event.duration_ms, 3)
                )

            def payload_number(event_type: str, key: str) -> float | None:
                event = first(event_type)
                value: Any = None if event is None else event.payload
                for part in key.split("."):
                    value = value.get(part) if isinstance(value, dict) else None
                return round(float(value), 3) if isinstance(value, (int, float)) else None

            ai_events = [
                item
                for item in events
                if item.type == "ai.request_complete" and item.duration_ms is not None
            ]
            # Composite duration contains its child durations: count leaf tools
            # once rather than inflating elapsed execution time on multi-step plans.
            tool_events = [
                item
                for item in events
                if item.type in {"tool.completed", "tool.failed"}
                and item.duration_ms is not None
                and item.payload.get("tool") != "agent.execute_plan"
            ]
            goals = [item for item in events if item.type == "plan.goal_verified"]
            completed_steps = [
                item.payload.get("step", {})
                for item in events
                if item.type == "plan.step_completed"
            ]
            first_event, last_event = events[0], events[-1]
            reports.append(
                {
                    "correlation_id": correlation_id,
                    "started_at": first_event.timestamp,
                    "stages_ms": {
                        "wake_detection": payload_number("wake.detected", "latency_ms"),
                        "wake_to_listening": payload_number(
                            "wake.command_capture_started", "wake_to_listening_ms"
                        ),
                        "capture": duration("voice.listening_stopped"),
                        "speech_to_text": duration("voice.transcription_complete"),
                        "stt_partial": duration("voice.transcription_partial"),
                        "reasoning_first_token": duration("ai.first_token"),
                        "barge_in_stop": payload_number(
                            "voice.barge_in", "interruption_latency_ms"
                        ),
                        "planning": payload_number("plan.created", "timings.planning"),
                        "model_reasoning": (
                            round(sum(float(item.duration_ms or 0.0) for item in ai_events), 3)
                            if ai_events
                            else None
                        ),
                        "tool_execution_and_verification": (
                            round(sum(float(item.duration_ms or 0.0) for item in tool_events), 3)
                            if tool_events
                            else None
                        ),
                        "command_total": duration("command.completed")
                        or duration("plan.completed"),
                        "tts_synthesis": payload_number("tts.started", "synthesis_latency_ms"),
                        "tts_startup": payload_number("tts.started", "startup_latency_ms"),
                        "tts_playback_total": duration("tts.completed"),
                        "observed_end_to_end": round(
                            max(0.0, last_event.monotonic - first_event.monotonic) * 1000, 3
                        ),
                    },
                    "events_observed": len(events),
                    "counts": {
                        "model_calls": sum(item.type == "ai.request_complete" for item in events),
                        "leaf_tool_results": len(tool_events),
                        "failed_leaf_tools": sum(
                            item.type == "tool.failed" or item.payload.get("ok") is False
                            for item in tool_events
                        ),
                        "verified_leaf_tools": sum(
                            item.payload.get("execution", {}).get("verified") is True
                            for item in tool_events
                        ),
                        "retries_in_completed_steps": sum(
                            max(0, int(step.get("attempts", 1)) - 1) for step in completed_steps
                        ),
                        "declared_goal_checks": len(goals),
                        "declared_goal_checks_passed": sum(
                            item.payload.get("verified") is True for item in goals
                        ),
                    },
                    "false_success_rate": None,
                    "metrics_scope": "Bounded event history, not an independently labelled end-to-end success benchmark",
                }
            )
        return {
            "recent": reports[-max(1, min(int(limit), 50)) :],
            "measurement_policy": "null means that stage was not observed for this correlation id",
        }

    @property
    def sequence(self) -> int:
        return self._sequence


# Old integrations still know this name. Let them through.
EventBus = PhaxEventBus
