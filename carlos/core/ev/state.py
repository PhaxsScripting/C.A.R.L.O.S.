from __future__ import annotations

from enum import StrEnum
from typing import Any

from .events import PhaxEventBus


class CoreState(StrEnum):
    DORMANT = "DORMANT"
    AWAKE = "AWAKE"
    LISTENING = "LISTENING"
    TRANSCRIBING = "TRANSCRIBING"
    THINKING = "THINKING"
    RETRIEVING_MEMORY = "RETRIEVING_MEMORY"
    USING_TOOL = "USING_TOOL"
    WAITING_FOR_CONFIRMATION = "WAITING_FOR_CONFIRMATION"
    SPEAKING = "SPEAKING"
    OFFLINE = "OFFLINE"
    ERROR = "ERROR"


ALLOWED_TRANSITIONS: dict[CoreState, set[CoreState]] = {
    CoreState.DORMANT: {
        CoreState.AWAKE,
        CoreState.LISTENING,
        CoreState.THINKING,
        CoreState.USING_TOOL,
        CoreState.WAITING_FOR_CONFIRMATION,
        CoreState.SPEAKING,
        CoreState.OFFLINE,
        CoreState.ERROR,
    },
    CoreState.AWAKE: {CoreState.LISTENING, CoreState.THINKING, CoreState.DORMANT, CoreState.ERROR},
    CoreState.LISTENING: {CoreState.TRANSCRIBING, CoreState.DORMANT, CoreState.ERROR},
    CoreState.TRANSCRIBING: {CoreState.THINKING, CoreState.DORMANT, CoreState.ERROR},
    CoreState.THINKING: {
        CoreState.RETRIEVING_MEMORY,
        CoreState.USING_TOOL,
        CoreState.WAITING_FOR_CONFIRMATION,
        CoreState.SPEAKING,
        CoreState.DORMANT,
        CoreState.OFFLINE,
        CoreState.ERROR,
    },
    CoreState.RETRIEVING_MEMORY: {
        CoreState.THINKING,
        CoreState.USING_TOOL,
        CoreState.DORMANT,
        CoreState.ERROR,
    },
    CoreState.USING_TOOL: {
        CoreState.THINKING,
        CoreState.WAITING_FOR_CONFIRMATION,
        CoreState.SPEAKING,
        CoreState.DORMANT,
        CoreState.ERROR,
    },
    CoreState.WAITING_FOR_CONFIRMATION: {
        CoreState.USING_TOOL,
        CoreState.THINKING,
        CoreState.DORMANT,
        CoreState.ERROR,
    },
    CoreState.SPEAKING: {CoreState.DORMANT, CoreState.LISTENING, CoreState.ERROR},
    CoreState.OFFLINE: {CoreState.DORMANT, CoreState.THINKING, CoreState.ERROR},
    CoreState.ERROR: {CoreState.DORMANT, CoreState.OFFLINE},
}


class StateMachine:
    def __init__(self, bus: PhaxEventBus) -> None:
        self.bus = bus
        self.current = CoreState.DORMANT
        self.detail = "Core initialized"

    def transition(
        self,
        target: CoreState,
        detail: str,
        correlation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if target == self.current:
            self.detail = detail
            return
        if target not in ALLOWED_TRANSITIONS[self.current]:
            raise ValueError(f"Invalid E.V. state transition: {self.current} -> {target}")
        previous = self.current
        self.current = target
        self.detail = detail
        self.bus.publish(
            "core.state_changed",
            "core",
            {"from": previous.value, "to": target.value, "detail": detail, **(metadata or {})},
            correlation_id,
        )

    def snapshot(self) -> dict[str, str]:
        return {"state": self.current.value, "detail": self.detail}
