from __future__ import annotations
from .action_claims import claims_computer_action

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable

from .ai import Provider, ProviderError, ProviderTurn, ToolCall
from .ai.base import ProviderDecisionError, ProviderReasoningTimeout, ProviderResourceError
from .events import PhaxEventBus
from .memory import MemoryStore
from .state import CoreState, StateMachine
from .tools.results import OBSERVATION_TOOLS

ToolRequester = Callable[[dict[str, Any], str | None], Awaitable[dict[str, Any]]]


class ReasoningBudgetExceeded(Exception):
    """A local task deadline, not evidence that the provider is offline."""


@dataclass(slots=True)
class PendingCommand:
    correlation_id: str
    turn: ProviderTurn
    outputs: list[tuple[ToolCall, dict[str, Any]]]
    pending_call: ToolCall
    remaining_calls: list[ToolCall]
    started_monotonic: float


class CommandEngine:
    def __init__(
        self,
        provider: Provider,
        bus: PhaxEventBus,
        state: StateMachine,
        memory: MemoryStore,
        tools: list[dict[str, Any]],
        request_tool: ToolRequester,
        context_turn_limit: int = 40,
        context_character_limit: int = 24_000,
    ) -> None:
        self.provider = provider
        self.bus = bus
        self.state = state
        self.memory = memory
        self.tools = tools
        self.request_tool = request_tool
        self.context_turn_limit = context_turn_limit
        self.context_character_limit = context_character_limit
        self.pending: dict[str, PendingCommand] = {}
        self._command_lock = asyncio.Lock()
        self._tool_history: dict[str, list[tuple[ToolCall, dict[str, Any]]]] = {}
        self.max_tool_rounds = 12
        self.max_tool_calls = 48
        self.task_timeout_seconds = 120.0
        self._wait_seconds: dict[str, float] = {}
        self.context_provider = None
        self.provider_guard = None
        self.stream_handler = None

    def _budget_elapsed(self, correlation, started):
        wall = time.monotonic() - started
        return (
            wall >= 3600
            or wall - min(1800, self._wait_seconds.get(correlation, 0)) > self.task_timeout_seconds
        )

    def _reasoning_remaining(self, correlation, started):
        wall = time.monotonic() - started
        return min(
            3600 - wall,
            self.task_timeout_seconds - wall + min(1800, self._wait_seconds.get(correlation, 0)),
        )

    async def _provider_turn(self, request, correlation, started):
        if self.provider_guard:
            self.provider_guard()
        remaining = self._reasoning_remaining(correlation, started)
        if remaining <= 0:
            raise ReasoningBudgetExceeded()
        deadline = asyncio.timeout(remaining)
        from .ai.streaming import observer

        def observe(kind, text, latency):
            if kind == "first_token":
                self.bus.publish("ai.first_token", "reasoning", {}, correlation, latency)
            elif kind == "sentence" and self.stream_handler:
                self.stream_handler(correlation, text, False)

        token = observer.set(observe)
        failed = True
        try:
            async with deadline:
                turn = await request()
                failed = False
        except TimeoutError as error:
            if deadline.expired():
                raise ReasoningBudgetExceeded() from error
            raise
        finally:
            observer.reset(token)
            if self.stream_handler:
                self.stream_handler(correlation, None, failed)
        if deadline.expired():
            raise ReasoningBudgetExceeded()
        return turn

    async def _invoke_tool(self, call: ToolCall, correlation: str) -> dict[str, Any]:
        started = time.monotonic()
        try:
            return await self.request_tool(
                {"name": call.name, "arguments": call.arguments, "correlation_id": correlation},
                correlation,
            )
        finally:
            if call.name == "agent.wait_for":
                self._wait_seconds[correlation] = (
                    self._wait_seconds.get(correlation, 0) + time.monotonic() - started
                )

    def provider_status(self) -> dict[str, Any]:
        available, reason = self.provider.available
        status = {
            "active": self.provider.name,
            "model": self.provider.model,
            "connected": available,
            "reason": reason,
        }
        transport_status = getattr(self.provider, "transport_status", None)
        if transport_status is not None:
            transport = transport_status()
            status.update(
                configured=available,
                transport=transport,
                connected=available and transport["state"] == "RESPONDED",
            )
            if available:
                status["reason"] = {
                    "UNTESTED": "Configured; connection not yet tested",
                    "REQUESTING": "Provider request in progress",
                    "RESPONDED": "Last provider HTTP request responded; not a continuous connection test",
                    "ERROR": "Last provider request failed; a new request can retry",
                    "CANCELLED": "Last provider request cancelled; connection unverified",
                }.get(transport["state"], "Connection unverified")
        return status

    async def relevant_memories(self, text: str) -> list[dict[str, Any]]:
        words = [
            word
            for word in re.findall(r"[A-Za-z0-9_.-]{4,}", text.casefold())
            if word not in {"what", "this", "that", "with", "from", "using"}
        ]
        if not words:
            return []
        all_memories = await asyncio.to_thread(self.memory.list_memories, "", 100)
        return [
            item
            for item in all_memories
            if any(word in item["content"].casefold() for word in words)
        ][:8]

    @asynccontextmanager
    async def _recover_failed_command(self, correlation_id: str):
        try:
            yield
        except BaseException as error:
            self._wait_seconds.pop(correlation_id, None)
            self._tool_history.pop(correlation_id, None)
            self.bus.publish(
                "command.failed",
                "language",
                {
                    "error": str(error),
                    "cancelled": isinstance(error, asyncio.CancelledError),
                },
                correlation_id,
            )
            if self.state.current != CoreState.DORMANT:
                if self.state.current != CoreState.ERROR:
                    self.state.transition(CoreState.ERROR, "Command interrupted", correlation_id)
                self.state.transition(
                    CoreState.DORMANT, "Ready for another request", correlation_id
                )
            raise
        finally:
            if not any(
                session.correlation_id == correlation_id for session in self.pending.values()
            ):
                self._wait_seconds.pop(correlation_id, None)
                self._tool_history.pop(correlation_id, None)

    async def submit(
        self, text: str, correlation_id: str | None = None, *, resume_context: str = ""
    ) -> dict[str, Any]:
        if not isinstance(text, str) or not text.strip() or len(text) > 8000:
            raise ValueError("command text must be 1-8000 characters")
        if self.state.current == CoreState.SPEAKING:
            return {"status": "busy", "message": "Carlos is still speaking the previous response."}
        if self._command_lock.locked():
            return {"status": "busy", "message": "Carlos is already handling another command."}
        correlation = correlation_id or uuid.uuid4().hex
        async with self._command_lock, self._recover_failed_command(correlation):
            started = time.monotonic()
            self._tool_history[correlation] = []
            clean_text = text.strip()
            await asyncio.to_thread(self.memory.add_conversation, correlation, "user", clean_text)
            self.bus.publish("command.received", "language", {"text": clean_text}, correlation)
            self.state.transition(CoreState.THINKING, "Interpreting command", correlation)

            self.state.transition(
                CoreState.RETRIEVING_MEMORY, "Searching explicit local memory", correlation
            )
            self.bus.publish(
                "memory.query_started", "memory", {"query_terms": "derived locally"}, correlation
            )
            memories = await self.relevant_memories(clean_text)
            if self.context_provider:
                try:
                    hints = await asyncio.wait_for(self.context_provider(clean_text), timeout=0.5)
                    memories = [*hints[:3], *memories][:8]
                except Exception:
                    self.bus.publish(
                        "context.unavailable",
                        "memory",
                        {
                            "reason": "Optional preference context unavailable; continuing without hints"
                        },
                        correlation,
                    )
            self.bus.publish(
                "memory.result",
                "memory",
                {"matches": len(memories), "ids": [item["id"] for item in memories]},
                correlation,
            )
            self.state.transition(CoreState.THINKING, "Memory context assembled", correlation)
            context = await asyncio.to_thread(
                self.memory.recent_conversation, self.context_turn_limit
            )
            while (
                sum(len(item["content"]) for item in context) > self.context_character_limit
                and len(context) > 2
            ):
                context.pop(0)

            self.bus.publish(
                "ai.request_started",
                "reasoning",
                {
                    "provider": self.provider.name,
                    "model": self.provider.model,
                    "context_turns": len(context),
                    "memory_matches": len(memories),
                },
                correlation,
            )
            try:
                prior_context = context[:-1]
                if resume_context:
                    prior_context = [*prior_context, {"role": "user", "content": resume_context}]
                turn = await self._provider_turn(
                    lambda: self.provider.begin(clean_text, prior_context, memories, self.tools),
                    correlation,
                    started,
                )
                self._emit_provider_turn(turn, correlation)
                return await self._advance(turn, [], correlation, started)
            except ReasoningBudgetExceeded:
                return await self._budget_exhausted(
                    ProviderTurn(self.provider.name, self.provider.model, ""),
                    correlation,
                    started,
                    "Reasoning time budget reached; pending model request cancelled",
                )
            except ProviderDecisionError as error:
                self.bus.publish(
                    "ai.decision_rejected", "reasoning", {"error": str(error)}, correlation
                )
                return await self._complete(
                    ProviderTurn(self.provider.name, self.provider.model, ""),
                    correlation,
                    started,
                    failure=(
                        str(error)
                        if isinstance(error, ProviderResourceError)
                        else (
                            "Local reasoning reached its time limit."
                            if isinstance(error, ProviderReasoningTimeout)
                            else "The model returned an invalid next step."
                        )
                    )
                    + " I stopped; prior actions, if any, remain in the execution receipts.",
                )
            except ProviderError as error:
                if self._tool_history.get(correlation):
                    return await self._complete(
                        ProviderTurn(self.provider.name, self.provider.model, ""),
                        correlation,
                        started,
                        failure=f"The provider request failed after earlier steps: {error}. Prior actions remain in the execution receipts; nothing was replayed.",
                    )
                self._tool_history.pop(correlation, None)
                self.bus.publish(
                    "ai.request_failed",
                    "reasoning",
                    {"provider": self.provider.name, "error": str(error)},
                    correlation,
                )
                self.state.transition(CoreState.OFFLINE, str(error), correlation)
                response = f"The configured AI provider is unavailable: {error}"
                await asyncio.to_thread(
                    self.memory.add_conversation, correlation, "assistant", response
                )
                # OFFLINE describes the provider failure event, not a permanent
                # lockout of deterministic tools, wake, and later retry.
                self.state.transition(
                    CoreState.DORMANT,
                    "Provider request failed; local command routes remain ready",
                    correlation,
                )
                return {"status": "offline", "correlation_id": correlation, "response": response}

    def _emit_provider_turn(self, turn: ProviderTurn, correlation: str) -> None:
        self.bus.publish(
            "ai.request_complete",
            "reasoning",
            {
                "provider": turn.provider,
                "model": turn.model,
                "response_id": turn.response_id,
                "interpreted_task": turn.interpreted_task,
                "plan": turn.plan,
                "tool_calls": len(turn.tool_calls),
                "usage": turn.usage,
            },
            correlation,
            turn.latency_ms,
        )

    async def _advance(
        self,
        turn: ProviderTurn,
        carried_outputs: list[tuple[ToolCall, dict[str, Any]]],
        correlation: str,
        started: float,
        depth: int = 0,
    ) -> dict[str, Any]:
        history = self._tool_history.setdefault(correlation, [])
        if depth > self.max_tool_rounds or self._budget_elapsed(correlation, started):
            return await self._budget_exhausted(turn, correlation, started)
        outputs = list(carried_outputs)
        for index, tool_call in enumerate(turn.tool_calls):
            if len(history) >= self.max_tool_calls or self._budget_elapsed(correlation, started):
                return await self._budget_exhausted(turn, correlation, started)
            # An unchanged request returning unchanged evidence three times is
            # not progress. Do not replay further model-selected mutations.
            if len(history) >= 3 and all(
                prior.name == tool_call.name
                and prior.arguments == tool_call.arguments
                and result.get("result") == history[-1][1].get("result")
                for prior, result in history[-3:]
            ):
                return await self._budget_exhausted(
                    turn,
                    correlation,
                    started,
                    "Repeated the same tool request without changing strategy",
                )
            result = await self._invoke_tool(tool_call, correlation)
            if result.get("status") == "cancelled":
                self._tool_history.pop(correlation, None)
                return {
                    "status": "cancelled",
                    "correlation_id": correlation,
                    "response": "Stopped. Remaining tool calls were not executed.",
                }
            if result.get("status") == "confirmation_required":
                confirmation = result["confirmation"]
                self.pending[confirmation["id"]] = PendingCommand(
                    correlation_id=correlation,
                    turn=turn,
                    outputs=outputs,
                    pending_call=tool_call,
                    remaining_calls=turn.tool_calls[index + 1 :],
                    started_monotonic=started,
                )
                response = f"I need {confirmation['permission'].lower()} permission before I can run {tool_call.name}."
                self.bus.publish(
                    "command.waiting_for_confirmation",
                    "security",
                    {"confirmation_id": confirmation["id"], "tool": tool_call.name},
                    correlation,
                )
                return {
                    "status": "confirmation_required",
                    "correlation_id": correlation,
                    "response": response,
                    "confirmation": confirmation,
                    "cognition": self.cognition(turn),
                }
            outputs.append((tool_call, result))
            history.append((tool_call, result))

        if turn.tool_calls:
            self.bus.publish(
                "ai.request_started",
                "reasoning",
                {"provider": turn.provider, "model": turn.model, "phase": "tool_results"},
                correlation,
            )
            turn = await self._provider_turn(
                lambda: self.provider.continue_with_tools(turn, outputs, self.tools),
                correlation,
                started,
            )
            self._emit_provider_turn(turn, correlation)
            return await self._advance(turn, [], correlation, started, depth + 1)
        return await self._complete(turn, correlation, started)

    async def resume_confirmation(
        self, confirmation_id: str, tool_result: dict[str, Any]
    ) -> dict[str, Any] | None:
        session = self.pending.pop(confirmation_id, None)
        if session is None:
            return None
        async with self._command_lock, self._recover_failed_command(session.correlation_id):
            self.state.transition(
                CoreState.THINKING, "Continuing after permission decision", session.correlation_id
            )
            outputs = session.outputs + [(session.pending_call, tool_result)]
            history = self._tool_history.setdefault(session.correlation_id, [])
            history.append((session.pending_call, tool_result))
            for index, tool_call in enumerate(session.remaining_calls):
                if len(history) >= self.max_tool_calls or self._budget_elapsed(
                    session.correlation_id, session.started_monotonic
                ):
                    return await self._budget_exhausted(
                        session.turn, session.correlation_id, session.started_monotonic
                    )
                result = await self._invoke_tool(tool_call, session.correlation_id)
                if result.get("status") == "cancelled":
                    self._tool_history.pop(session.correlation_id, None)
                    return {
                        "status": "cancelled",
                        "correlation_id": session.correlation_id,
                        "response": "Stopped. Remaining tool calls were not executed.",
                    }
                if result.get("status") == "confirmation_required":
                    confirmation = result["confirmation"]
                    self.pending[confirmation["id"]] = PendingCommand(
                        correlation_id=session.correlation_id,
                        turn=session.turn,
                        outputs=outputs,
                        pending_call=tool_call,
                        remaining_calls=session.remaining_calls[index + 1 :],
                        started_monotonic=session.started_monotonic,
                    )
                    return {"status": "confirmation_required", "confirmation": confirmation}
                outputs.append((tool_call, result))
                history.append((tool_call, result))
            try:
                next_turn = await self._provider_turn(
                    lambda: self.provider.continue_with_tools(session.turn, outputs, self.tools),
                    session.correlation_id,
                    session.started_monotonic,
                )
                self._emit_provider_turn(next_turn, session.correlation_id)
                return await self._advance(
                    next_turn, [], session.correlation_id, session.started_monotonic, 1
                )
            except ReasoningBudgetExceeded:
                return await self._budget_exhausted(
                    session.turn,
                    session.correlation_id,
                    session.started_monotonic,
                    "Reasoning time budget reached; pending model request cancelled",
                )
            except ProviderDecisionError as error:
                self.bus.publish(
                    "ai.decision_rejected",
                    "reasoning",
                    {"error": str(error)},
                    session.correlation_id,
                )
                return await self._complete(
                    session.turn,
                    session.correlation_id,
                    session.started_monotonic,
                    failure=(
                        str(error)
                        if isinstance(error, ProviderResourceError)
                        else (
                            "Local reasoning reached its time limit."
                            if isinstance(error, ProviderReasoningTimeout)
                            else "The model returned an invalid next step after execution."
                        )
                    )
                    + " I stopped; prior actions remain in the execution receipts.",
                )
            except ProviderError as error:
                if self._tool_history.get(session.correlation_id):
                    return await self._complete(
                        session.turn,
                        session.correlation_id,
                        session.started_monotonic,
                        failure=f"The provider request failed after earlier steps: {error}. Prior actions remain in the execution receipts; nothing was replayed.",
                    )
                self._tool_history.pop(session.correlation_id, None)
                self.bus.publish(
                    "ai.request_failed",
                    "reasoning",
                    {"provider": self.provider.name, "error": str(error)},
                    session.correlation_id,
                )
                self.state.transition(CoreState.OFFLINE, str(error), session.correlation_id)
                response = f"The configured AI provider became unavailable: {error}"
                await asyncio.to_thread(
                    self.memory.add_conversation, session.correlation_id, "assistant", response
                )
                self.state.transition(
                    CoreState.DORMANT,
                    "Provider request failed; local command routes remain ready",
                    session.correlation_id,
                )
                return {
                    "status": "offline",
                    "correlation_id": session.correlation_id,
                    "response": response,
                }

    async def _budget_exhausted(
        self,
        turn: ProviderTurn,
        correlation: str,
        started: float,
        reason: str = "Task execution budget reached",
    ) -> dict[str, Any]:
        response = f"I stopped: {reason}. The overall task is not confirmed complete."
        self.bus.publish("command.budget_exhausted", "language", {"reason": reason}, correlation)
        return await self._complete(turn, correlation, started, failure=response)

    async def _complete(
        self, turn: ProviderTurn, correlation: str, started: float, *, failure: str = ""
    ) -> dict[str, Any]:
        self._wait_seconds.pop(correlation, None)
        response = turn.text.strip() or "The request completed without a text response."
        history = self._tool_history.pop(correlation, [])
        failed = []
        recovered = 0
        for index, (call, result) in enumerate(history):
            if result.get("status") == "completed":
                continue

            # A later verified retry of the same operation can resolve that
            # failure. Structured recovery may change steps, but must preserve
            # exactly the same declared goal and postconditions.
            def repairs(later_call, later_result):
                if later_result.get("status") != "completed" or not later_result.get(
                    "execution", later_result.get("verification", {})
                ).get("verified"):
                    return False
                # A retired endpoint performed no action. Its actual approved
                # replacement can resolve that failure, but inspecting a
                # project alone cannot, nor can a build of a different project.
                if (
                    call.name == "development.build_project"
                    and result.get("result", {}).get("error_code") == "retired_build_endpoint"
                    and result.get("result", {}).get("executed") is False
                ):
                    return (
                        later_call.name == "development.project.run"
                        and later_call.arguments.get("operation") == "build"
                        and later_call.arguments.get("project") == call.arguments.get("project")
                    )
                if call.name != later_call.name:
                    return False
                if call.name == "agent.execute_plan":
                    return all(
                        call.arguments.get(key) == later_call.arguments.get(key)
                        for key in ("goal", "conditions")
                    )
                return call.arguments == later_call.arguments

            if any(repairs(c, r) for c, r in history[index + 1 :]):
                recovered += 1
            else:
                failed.append((call, result))
        read_only = OBSERVATION_TOOLS | {
            item["name"] for item in self.tools if item.get("read_only")
        }
        mutations = [(call, result) for call, result in history if call.name not in read_only]
        status, execution_status = "completed", "ANSWERED"
        if failure or failed:
            status, execution_status = "failed", "FAILED"
            response = (
                failure or f"I couldn't finish the task: {failed[-1][0].name} did not succeed."
            )
        elif mutations:
            verified = sum(
                bool(result.get("execution", result.get("verification", {})).get("verified"))
                for _, result in mutations
            )
            # A model's final prose cannot certify a goal. Produce a factual
            # execution receipt; preserve raw model text only in provider events.
            execution_status = "EXECUTED_UNVERIFIED"
            response = (
                f"Verified {verified} tool action{'s' if verified != 1 else ''}. "
                if verified
                else "The requested actions were sent. "
            )
            response += "The overall task outcome has not been independently verified."
            if all(
                call.name == "agent.execute_plan"
                for call, result in mutations
                if result.get("status") == "completed"
            ):
                verified_plans = [
                    result
                    for call, result in mutations
                    if (result.get("result", {}).get("goal_verification") or {}).get("verified")
                ]
                if verified_plans:
                    execution_status = "DECLARED_CONDITIONS_VERIFIED"
                    response = "The plan's completion conditions are verified." + (
                        " Recovered from the earlier failure." if recovered else ""
                    )
        elif not mutations and claims_computer_action(response):
            status, execution_status = "failed", "BLOCKED"
            response = "No computer action was executed for that request."
        await asyncio.to_thread(self.memory.add_conversation, correlation, "assistant", response)
        if self.state.current not in {CoreState.DORMANT, CoreState.SPEAKING}:
            self.state.transition(CoreState.DORMANT, "Command completed", correlation)
        duration_ms = (time.monotonic() - started) * 1000
        self.bus.publish(
            "command.completed" if status == "completed" else "command.failed",
            "language",
            {
                "response": response,
                "execution_status": execution_status,
                "tool_calls": len(history),
            },
            correlation,
            duration_ms,
        )
        return {
            "status": status,
            "execution_status": execution_status,
            "goal_verified": False,
            "recovered_failures": recovered,
            "tool_receipts": [
                {
                    "tool": call.name,
                    "status": result.get("status"),
                    "execution": result.get("execution", result.get("verification", {})),
                }
                for call, result in history
            ],
            "correlation_id": correlation,
            "response": response,
            "cognition": self.cognition(turn),
            "duration_ms": round(duration_ms, 3),
        }

    @staticmethod
    def cognition(turn: ProviderTurn) -> dict[str, Any]:
        return {
            "provider": turn.provider,
            "model": turn.model,
            "interpreted_task": turn.interpreted_task,
            "plan": turn.plan,
            "usage": turn.usage,
            "latency_ms": round(turn.latency_ms, 3),
            "response_id": turn.response_id,
        }
