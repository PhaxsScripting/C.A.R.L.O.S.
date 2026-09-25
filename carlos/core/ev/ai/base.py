from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class ProviderError(RuntimeError):
    pass


class ProviderDecisionError(ProviderError):
    """The model responded, but its decision was unusable; not an outage."""

    pass


class ProviderReasoningTimeout(ProviderDecisionError):
    """The bounded reasoning turn expired without proving a provider outage."""

    pass


class ProviderResourceError(ProviderDecisionError):
    """Local inference stopped to protect desktop thermal or memory headroom."""

    pass


@dataclass(slots=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class ProviderTurn:
    provider: str
    model: str
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    interpreted_task: str = ""
    plan: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0
    continuation: Any = None
    response_id: str = ""


class Provider(ABC):
    name = "base"

    @property
    @abstractmethod
    def available(self) -> tuple[bool, str]:
        raise NotImplementedError

    @property
    @abstractmethod
    def model(self) -> str:
        raise NotImplementedError

    @abstractmethod
    async def begin(
        self,
        user_text: str,
        context: list[dict[str, Any]],
        memories: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ProviderTurn:
        raise NotImplementedError

    @abstractmethod
    async def continue_with_tools(
        self,
        turn: ProviderTurn,
        outputs: list[tuple[ToolCall, dict[str, Any]]],
        tools: list[dict[str, Any]],
    ) -> ProviderTurn:
        raise NotImplementedError
