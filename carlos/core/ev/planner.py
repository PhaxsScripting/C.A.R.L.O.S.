from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Awaitable, Callable
from urllib.parse import quote_plus

from .ai.offline import is_desktop_input_request, mask_desktop_literals, parse_desktop_input
from .events import PhaxEventBus
from .commands import clock_request, direct_action, request_text, browser_input
from .state import CoreState, StateMachine
from .tools import ToolRegistry
from .tools.results import OBSERVATION_TOOLS, evaluate_result

ToolRequester = Callable[[dict[str, Any], str | None], Awaitable[dict[str, Any]]]


class StepStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"


@dataclass(slots=True)
class PlanStep:
    id: str
    tool: str
    arguments: dict[str, Any]
    dependencies: list[str] = field(default_factory=list)
    expected_postcondition: str = ""
    permission_class: str = ""
    status: StepStatus = StepStatus.PENDING
    resolved_arguments: dict[str, Any] | None = None
    actual_result: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None
    duration_ms: float | None = None
    attempts: int = 0
    error: str = ""


@dataclass(slots=True)
class TaskPlan:
    id: str
    correlation_id: str
    request: str
    goal: str
    steps: list[PlanStep]
    dry_run: bool = False
    status: str = "PENDING"
    current_step: int = 0
    created_monotonic: float = field(default_factory=time.monotonic)
    duration_ms: float | None = None
    cancellation_reason: str = ""
    capability_gaps: list[dict[str, Any]] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)
    world_state_used: dict[str, Any] = field(default_factory=dict)
    recovery: list[dict[str, Any]] = field(default_factory=list)
    failure_report: dict[str, Any] | None = None
    goal_conditions: list[dict[str, Any]] = field(default_factory=list)
    goal_verification: dict[str, Any] | None = None
    goal_source: str = "unspecified"

    def public(self) -> dict[str, Any]:
        return asdict(self)


def _clean_entity(value: str) -> str:
    # Exact references are opaque identities, not prose containing "window".
    if value.strip().startswith("window-id:"):
        return value.strip()
    cleaned = re.sub(
        r"\b(?:please|for me|window|app|application)\b", " ", value, flags=re.IGNORECASE
    )
    cleaned = re.sub(r"\bback\s*$", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.!?")
    return cleaned or value.strip(" ,.!?")


def _entity_key(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", _clean_entity(value).casefold())) - {"a", "an", "the", "my"}


class TaskPlanner:
    """Small deterministic planner for composable desktop operations.

    It does not expose model-generated shell. Plans contain registered typed
    tools, data dependencies, expected postconditions, and measured results.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        request_tool: ToolRequester,
        bus: PhaxEventBus,
        state: StateMachine,
    ) -> None:
        self.registry = registry
        self.request_tool = request_tool
        self.bus = bus
        self.state = state
        self.active: TaskPlan | None = None
        self.recent: list[TaskPlan] = []
        self.pending: dict[str, tuple[TaskPlan, int]] = {}
        self.external_pending: dict[str, tuple[TaskPlan, int]] = {}
        self.last_entities: dict[str, Any] = {}
        self.saved_routines: dict[str, list[str]] = {}
        self.last_window_operation: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    def snapshot(self) -> dict[str, Any]:
        return {
            "active": None if self.active is None else self.active.public(),
            "recent": [plan.public() for plan in self.recent[-12:]],
        }

    def capability_gap(
        self, required: str, reason: str, gap_type: str = "MISSING_TOOL"
    ) -> dict[str, Any]:
        if required.startswith("spotify.") and "HTTP 403" in reason:
            return {
                "required": required,
                "type": "EXTERNAL_ACCOUNT_ACCESS",
                "reason": reason,
                "possible_solution": "Reconnect Spotify in Phaxity Audio and check the Spotify developer app's allowed users and account eligibility. Changing E.V. computer permissions will not fix Spotify's rejection.",
                "requires_user_approval": False,
                "requires_external_account_action": True,
            }
        return {
            "required": required,
            "type": gap_type,
            "reason": reason,
            "possible_solution": "Create a checkpointed E.V. engineering task for review",
            "requires_user_approval": True,
        }

    def _settle_terminal_state(self, detail: str, correlation_id: str) -> None:
        if self.state.current in {
            CoreState.THINKING,
            CoreState.RETRIEVING_MEMORY,
            CoreState.USING_TOOL,
            CoreState.ERROR,
        }:
            self.state.transition(CoreState.DORMANT, detail, correlation_id)

    def _step(
        self,
        step_id: str,
        tool: str,
        arguments: dict[str, Any],
        expected: str,
        deps: list[str] | None = None,
    ) -> PlanStep:
        try:
            permission = self.registry.get(tool).permission.value
        except Exception:
            permission = "UNKNOWN"
        return PlanStep(step_id, tool, arguments, deps or [], expected, permission)

    def _resolve_window_pronoun(self, description: str) -> str:
        cleaned = _clean_entity(description)
        if cleaned.casefold() not in {"it", "that", "this", "the one"}:
            return cleaned
        previous = self.last_entities.get("window")
        if not isinstance(previous, dict):
            return "window-id:missing-context"
        if time.monotonic() - float(self.last_entities.get("window_at", 0)) > 300:
            return "window-id:expired-context"
        if previous.get("id"):
            return f"window-id:{previous['id']}"
        return str(
            previous.get("app_id")
            or previous.get("resource_class")
            or previous.get("title")
            or cleaned
        )

    @staticmethod
    def _browser_search_request(text: str) -> tuple[str, str] | None:
        browser = r"(?:firefox|chromium|chrome|brave|vivaldi|edge|opera)"
        prefix = r"(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?"
        opening = re.fullmatch(
            prefix + rf"(?:open|launch|start)\s+(?:(?:my|the)\s+)?({browser})(?:\s+browser)?"
            r"\s*(?:[,;]|and|then)\s*(?:(?:open\s+(?:it\s+)?(?:in\s*,?\s*)?(?:a\s+)?new\s+tab)\s*(?:and|then|,)\s*)?"
            r"(?:search(?:\s+for)?|look\s+up)\s+(.+?)[.!?]*",
            text.strip(),
            re.IGNORECASE,
        )
        searching = re.fullmatch(
            prefix
            + rf"(?:search(?:\s+for)?|look\s+up)\s+(.+?)\s+(?:in|using|on)\s+({browser})(?:\s+browser)?[.!?]*",
            text.strip(),
            re.IGNORECASE,
        )
        if opening:
            return opening.group(1), opening.group(2).strip(' "“”')
        if searching:
            return searching.group(2), searching.group(1).strip(' "“”')
        return None

    def try_plan(self, text: str, correlation_id: str) -> TaskPlan | None:
        original = text.strip()
        if clock_request(original):
            return TaskPlan(
                uuid.uuid4().hex,
                correlation_id,
                original,
                "Read the current system clock",
                [
                    self._step(
                        "clock", "system.clock", {}, "Actual local time, date and timezone returned"
                    )
                ],
            )
        if request_text(original) is None and not re.search(
            r"\b(?:dry run|don't actually do it|do not actually do it|just show me)\b",
            original,
            re.I,
        ):
            return None
        lowered = mask_desktop_literals(original).casefold()
        dry_run = bool(
            re.search(
                r"\b(?:dry run|don't actually|do not actually|what would you do|just show me)\b",
                lowered,
            )
        )
        request = original
        for marker in reversed(
            list(
                re.finditer(
                    r"\b(?:dry run|don't actually do it|do not actually do it|just show me)\b",
                    lowered,
                )
            )
        ):
            request = request[: marker.start()] + request[marker.end() :]
        request = request_text(request.strip(" ,")) or request.strip(" ,")

        hand_command = re.fullmatch(
            r"(pause|resume) (?:holohand|hand gestures)[.!?]*", request, re.I
        )
        if hand_command:
            return TaskPlan(
                uuid.uuid4().hex,
                correlation_id,
                original,
                "Control HoloHand gesture input",
                [
                    self._step(
                        "holohand",
                        "holohand.set_paused",
                        {"paused": hand_command[1].casefold() == "pause"},
                        "Running HoloHand state read back",
                    )
                ],
                dry_run,
            )
        save_setup = re.fullmatch(
            r"save (?:this|the current) (?:setup|workspace) as ([A-Za-z0-9][A-Za-z0-9 _.-]{0,99})",
            request,
            re.I,
        )
        if save_setup:
            return TaskPlan(
                uuid.uuid4().hex,
                correlation_id,
                original,
                "Save the current window setup",
                [
                    self._step(
                        "save_setup",
                        "workspaces.capture_current",
                        {"name": save_setup[1].strip()},
                        "Saved window metadata read back",
                    )
                ],
                dry_run,
            )
        correction = re.fullmatch(
            r"(?:no[, ]+)?(?:i\s+(?:meant|mean)\s+)?(?:the\s+)?other\s+(.+?)(?:\s+window)?[.!?]*",
            request,
            re.I,
        )
        previous_window = self.last_entities.get("window", {})
        if (
            correction
            and self.last_window_operation
            and previous_window.get("id")
            and time.monotonic() - float(self.last_entities.get("window_at", 0)) <= 300
        ):
            operation = self.last_window_operation
            arguments = {**operation["arguments"], "window_id": {"$ref": "window.result.window.id"}}
            steps = [
                self._step(
                    "window",
                    "desktop.window.resolve",
                    {"description": correction[1], "exclude_window_id": previous_window["id"]},
                    "One different exact window",
                ),
                self._step(
                    "correction",
                    operation["tool"],
                    arguments,
                    "The corrected reversible window action is verified",
                    ["window"],
                ),
            ]
            return TaskPlan(
                uuid.uuid4().hex,
                correlation_id,
                original,
                "Apply the last window action to the other requested window",
                steps,
                dry_run,
            )

        navigation = browser_input(request)
        action = None if navigation else direct_action(request)
        choice = re.fullmatch(
            r"(use|open|read|complete|finish|copy|archive|restore|click|activate) (?:the )?(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|\d+)(?: one| item| result| control| button| link)?",
            request,
            re.I,
        )
        if choice:
            from .commands import Action

            saved = self.last_entities.get("choices", {})
            words = "first second third fourth fifth sixth seventh eighth ninth tenth".split()
            index = (
                words.index(choice[2].lower()) if choice[2].lower() in words else int(choice[2]) - 1
            )
            items = saved.get("items", [])
            kind, verb = saved.get("kind"), choice[1].lower()
            mapping = {
                "note": {
                    "read": "notes.read",
                    "use": "notes.read",
                    "open": "notes.read",
                    "archive": "notes.archive",
                    "restore": "notes.restore",
                },
                "task": {
                    "read": "tasks.read",
                    "use": "tasks.read",
                    "open": "tasks.read",
                    "complete": "tasks.complete",
                    "finish": "tasks.complete",
                    "archive": "tasks.archive",
                    "restore": "tasks.restore",
                },
                "bookmark": {
                    "open": "bookmarks.open",
                    "use": "bookmarks.open",
                    "read": "bookmarks.read",
                    "archive": "bookmarks.archive",
                    "restore": "bookmarks.restore",
                },
                "snippet": {
                    "read": "snippets.read",
                    "use": "snippets.read",
                    "copy": "snippets.copy",
                    "archive": "snippets.archive",
                    "restore": "snippets.restore",
                },
                "file": {"open": "files.open_selected", "use": "files.open_selected"},
                "control": {
                    "click": "controls.activate",
                    "activate": "controls.activate",
                    "use": "controls.activate",
                },
            }
            tool = mapping.get(kind, {}).get(verb)
            if tool and time.monotonic() - saved.get("at", 0) <= 120 and 0 <= index < len(items):
                item = items[index]
                args = (
                    {"identifier": item["id"]}
                    if kind not in {"file", "control"}
                    else (
                        {"path": item["path"], "fingerprint": item["fingerprint"]}
                        if kind == "file"
                        else {
                            "path": item["id"],
                            **{
                                key: item[key]
                                for key in (
                                    "window_id",
                                    "process_id",
                                    "window_title",
                                    "name",
                                    "role",
                                    "application",
                                )
                            },
                        }
                    )
                )
                action = Action(tool, args)
            else:
                action = Action("interaction.selection_status", {})
        if action:
            if action.tool == "controls.list":
                steps = [
                    self._step(
                        "window",
                        "desktop.window.resolve",
                        {
                            "description": self._resolve_window_pronoun(
                                action.arguments["description"]
                            )
                        },
                        "One exact requested window",
                    ),
                    self._step(
                        "focus",
                        "desktop.window.activate",
                        {"window_id": {"$ref": "window.result.window.id"}},
                        "Requested window focused",
                        ["window"],
                    ),
                    self._step(
                        "controls",
                        "desktop.controls.list",
                        {
                            "window_id": {"$ref": "window.result.window.id"},
                            "role": action.arguments["role"],
                        },
                        "Numbered native controls read from the exact window",
                        ["focus"],
                    ),
                ]
            elif action.tool == "controls.activate":
                steps = [
                    self._step(
                        "focus",
                        "desktop.window.activate",
                        {"window_id": action.arguments["window_id"]},
                        "The previously listed window is focused",
                    ),
                    self._step(
                        "control",
                        "desktop.controls.activate",
                        action.arguments,
                        "Exact semantic control accepts activation; result not verified",
                        ["focus"],
                    ),
                ]
            elif action.tool == "editing.key":
                steps = [
                    self._step(
                        "window",
                        "desktop.window.resolve",
                        {
                            "description": self._resolve_window_pronoun(
                                action.arguments["description"]
                            )
                        },
                        "One exact active window",
                    ),
                    self._step(
                        "connect",
                        "desktop.input.connect",
                        {},
                        "A native keyboard input session is connected",
                        ["window"],
                    ),
                    self._step(
                        "focus",
                        "desktop.window.activate",
                        {"window_id": {"$ref": "window.result.window.id"}},
                        "Focus restored to the exact window after native consent",
                        ["connect"],
                    ),
                    self._step(
                        "edit",
                        "desktop.keyboard.key",
                        {
                            "window_id": {"$ref": "window.result.window.id"},
                            "key": action.arguments["key"],
                            "modifiers": action.arguments["modifiers"],
                        },
                        "Requested shortcut delivered; document outcome not verified",
                        ["focus"],
                    ),
                ]
            elif action.tool in {"browser.video", "browser.shortcut"}:
                target = self._resolve_window_pronoun(action.arguments["description"])
                steps = [
                    self._step(
                        "window",
                        "desktop.window.resolve",
                        {"description": target},
                        "One exact browser window",
                        [],
                    ),
                    self._step(
                        "focus",
                        "desktop.window.activate",
                        {"window_id": {"$ref": "window.result.window.id"}},
                        "Exact browser window focused",
                        ["window"],
                    ),
                    self._step(
                        "browser",
                        action.tool,
                        {
                            "window_id": {"$ref": "window.result.window.id"},
                            "action": action.arguments["action"],
                        },
                        (
                            "Video state verified"
                            if action.tool == "browser.video"
                            else "Browser input delivered; page outcome not yet verified"
                        ),
                        ["focus"],
                    ),
                ]
            elif action.tool == "window.state":
                target = self._resolve_window_pronoun(action.arguments["description"])
                steps = [
                    self._step(
                        "window",
                        "desktop.window.resolve",
                        {"description": target},
                        "One exact requested window",
                    ),
                    self._step(
                        "state",
                        "desktop.window.state",
                        {
                            "window_id": {"$ref": "window.result.window.id"},
                            "state": action.arguments["action"],
                        },
                        "Requested window state is verified",
                        ["window"],
                    ),
                ]
            elif action.tool == "routines.run":
                commands = self.saved_routines.get(action.arguments["name"].casefold())
                if not commands:
                    return None
                steps = []
                for index, command in enumerate(commands):
                    nested = direct_action(command)
                    if nested and nested.tool in {"routines.run", "system.power"}:
                        return None
                    self._expanding_routine = True
                    try:
                        subplan = self.try_plan(command, correlation_id)
                    finally:
                        self._expanding_routine = False
                    if subplan is None:
                        return None
                    mapping = {step.id: f"routine_{index}_{step.id}" for step in subplan.steps}

                    def remap(value):
                        if isinstance(value, dict):
                            if set(value) == {"$ref"}:
                                head, tail = value["$ref"].split(".", 1)
                                return {"$ref": mapping[head] + "." + tail}
                            return {key: remap(item) for key, item in value.items()}
                        if isinstance(value, list):
                            return [remap(item) for item in value]
                        return value

                    previous = steps[-1].id if steps else None
                    for step in subplan.steps:
                        step.id = mapping[step.id]
                        step.arguments = remap(step.arguments)
                        step.dependencies = [mapping[item] for item in step.dependencies]
                        if previous and not step.dependencies:
                            step.dependencies = [previous]
                        steps.append(step)
            else:
                steps = [
                    self._step(
                        "action",
                        action.tool,
                        action.arguments,
                        "The tool reports the actual result",
                    )
                ]
            return TaskPlan(uuid.uuid4().hex, correlation_id, original, request, steps, dry_run)

        desktop_input = navigation or parse_desktop_input(request)
        browser_search = self._browser_search_request(request)
        if browser_search:
            application, search_text = browser_search
            url = "https://www.google.com/search?q=" + quote_plus(search_text)
            if len(url) > 2000:
                return None
            return TaskPlan(
                uuid.uuid4().hex,
                correlation_id,
                original,
                request,
                [
                    self._step(
                        "open_url",
                        "browser.open_url",
                        {"url": url, "browser": application.lower()},
                        "The browser accepts the exact search URL",
                    )
                ],
                dry_run,
            )
        if desktop_input:
            steps: list[PlanStep] = []
            active_description = ""
            window_ref: dict[str, str] = {}
            if browser_search:
                application = browser_search[0]
                steps.extend(
                    [
                        self._step(
                            "application",
                            "applications.list",
                            {"query": application, "limit": 8, "launch_only": True},
                            "One high-confidence installed browser",
                        ),
                        self._step(
                            "launch",
                            "applications.open",
                            {
                                "desktop_id": {
                                    "$ref": "application.result.applications.0.desktop_id"
                                }
                            },
                            "Browser launch request succeeds",
                            ["application"],
                        ),
                        self._step(
                            "browser_window",
                            "desktop.window.wait",
                            {"description": application, "timeout_seconds": 8},
                            "One exact browser window",
                            ["launch"],
                        ),
                    ]
                )
                active_description = application
                window_ref = {"$ref": "browser_window.result.window.id"}
            connected = False
            for index, action in enumerate(desktop_input["actions"]):
                description, tool = self._resolve_window_pronoun(action["window"]), action["tool"]
                needs_portal = tool.startswith(("desktop.pointer.", "desktop.keyboard."))
                if description != active_description:
                    step_id = f"window_{index}"
                    steps.append(
                        self._step(
                            step_id,
                            "desktop.window.resolve",
                            {"description": description},
                            "One exact live target window",
                            [steps[-1].id] if steps else [],
                        )
                    )
                    window_ref = {"$ref": f"{step_id}.result.window.id"}
                    active_description = description
                if needs_portal and not connected:
                    steps.append(
                        self._step(
                            "input_session",
                            "desktop.input.connect",
                            {},
                            "A native desktop input session is connected",
                            [steps[-1].id],
                        )
                    )
                    connected = True
                # The portal's native dialog can take focus. Activate afterward,
                # and before every action, using the already-resolved exact ID.
                steps.append(
                    self._step(
                        f"focus_{index}",
                        "desktop.window.activate",
                        {"window_id": window_ref},
                        "KWin reports the exact target window focused",
                        [steps[-1].id],
                    )
                )
                if tool == "desktop.window.activate":
                    continue
                arguments = dict(action["arguments"])
                if needs_portal:
                    arguments["window_id"] = window_ref
                    if tool == "desktop.pointer.scroll" and "x" not in arguments:
                        cursor_id = f"cursor_{index}"
                        steps.append(
                            self._step(
                                cursor_id,
                                "desktop.world",
                                {},
                                "Read the current cursor position before scrolling",
                                [steps[-1].id],
                            )
                        )
                        arguments.update(
                            x={"$ref": f"{cursor_id}.result.cursor.x"},
                            y={"$ref": f"{cursor_id}.result.cursor.y"},
                        )
                elif tool.startswith("accessibility.element."):
                    arguments["window_id"] = window_ref
                expected = "Input is delivered to the exact focused window; application outcome needs observation"
                if tool == "desktop.pointer.move":
                    expected = "KWin reports the pointer at the requested logical coordinates"
                elif tool == "accessibility.element.set_text":
                    expected = "Read back the text of one unambiguous named editable field"
                elif tool == "accessibility.element.activate":
                    expected = "An unambiguous semantic control accepts the action; application outcome needs observation"
                steps.append(
                    self._step(f"input_{index}", tool, arguments, expected, [steps[-1].id])
                )
            goal = (
                "Open the requested browser and submit the search in a new tab"
                if browser_search
                else "Carry out explicit desktop input in the requested windows"
            )
            return TaskPlan(uuid.uuid4().hex, correlation_id, original, goal, steps, dry_run)
        if is_desktop_input_request(request):
            # Never reinterpret a literal typing payload as a window command.
            return None

        # General geometry transfer: the nouns are resolved from live KWin
        # metadata; application names are never enumerated here.
        transfer = re.fullmatch(
            r"(?:make|put|match)\s+(.+?)\s+(?:the\s+)?(?:same\s+(?:size(?:\s+and\s+(?:position|coordinates))?|geometry)\s+as|match(?:es)?\s+)(.+?)(?:\s+(?:and\s+)?(?:then\s+)?close\s+(.+?))?[.!?]?$",
            request,
            re.IGNORECASE,
        )
        where = re.fullmatch(
            r"put\s+(.+?)\s+(?:exactly\s+)?where\s+(.+?)\s+is(?:\s+(?:and\s+)?(?:then\s+)?close\s+(.+?))?[.!?]?$",
            request,
            re.IGNORECASE,
        )
        copy_match = re.fullmatch(
            r"(?:copy|take)\s+(?:the\s+)?(?:size\s+and\s+(?:coordinates|coords|position)|geometry)\s+of\s+(.+?),?\s*(?:then\s+)?make\s+(.+?)\s+match\s+(?:it|that)(?:,?\s*(?:and\s+)?(?:then\s+)?close\s+(.+?))?[.!?]?$",
            request,
            re.IGNORECASE,
        )
        if copy_match:
            source, target, close_target = (
                copy_match.group(1),
                copy_match.group(2),
                copy_match.group(3),
            )
        elif where:
            target, source, close_target = where.group(1), where.group(2), where.group(3)
        elif transfer:
            target, source, close_target = transfer.group(1), transfer.group(2), transfer.group(3)
        else:
            target = source = close_target = None
        if target and source:
            steps = [
                self._step(
                    "source",
                    "desktop.window.resolve",
                    {"description": _clean_entity(source)},
                    "One unambiguous source window",
                ),
                self._step(
                    "target",
                    "desktop.window.resolve",
                    {"description": _clean_entity(target)},
                    "One unambiguous target window",
                ),
                self._step(
                    "transfer_geometry",
                    "desktop.window.move_resize",
                    {
                        "window_id": {"$ref": "target.result.window.id"},
                        "x": {"$ref": "source.result.window.geometry.x"},
                        "y": {"$ref": "source.result.window.geometry.y"},
                        "width": {"$ref": "source.result.window.geometry.width"},
                        "height": {"$ref": "source.result.window.geometry.height"},
                    },
                    "Target frame geometry matches source within KWin tolerance",
                    ["source", "target"],
                ),
            ]
            if close_target:
                clean_close = _clean_entity(close_target)
                ref = (
                    "source.result.window.id"
                    if _entity_key(source) == _entity_key(clean_close)
                    else None
                )
                if ref:
                    close_args: dict[str, Any] = {"window_id": {"$ref": ref}}
                    deps = ["transfer_geometry"]
                else:
                    steps.append(
                        self._step(
                            "close_target",
                            "desktop.window.resolve",
                            {"description": clean_close},
                            "One exact close target",
                            ["transfer_geometry"],
                        )
                    )
                    close_args = {"window_id": {"$ref": "close_target.result.window.id"}}
                    deps = ["close_target"]
                steps.append(
                    self._step(
                        "close",
                        "desktop.window.close",
                        close_args,
                        "The exact KWin window ID no longer exists",
                        deps,
                    )
                )
            return TaskPlan(
                uuid.uuid4().hex,
                correlation_id,
                original,
                f"Transfer window geometry from {_clean_entity(source)} to {_clean_entity(target)}",
                steps,
                dry_run,
            )

        if re.fullmatch(
            r"(?:please\s+)?(?:undo|revert)(?:\s+(?:that|it|the\s+last\s+(?:window\s+)?change|the\s+last\s+(?:move|resize|layout)))?[.!?]*",
            request,
            re.IGNORECASE,
        ):
            steps = [
                self._step(
                    "undo",
                    "desktop.window.undo_last",
                    {},
                    "The previous E.V. window state is restored",
                ),
            ]
            return TaskPlan(
                uuid.uuid4().hex,
                correlation_id,
                original,
                "Undo the last reversible window change",
                steps,
                dry_run,
            )

        centered = re.fullmatch(
            r"(?:center|centre)\s+(.+?)(?:\s+(?:window|app|application))?[.!?]?$",
            request,
            re.IGNORECASE,
        )
        tiled = re.fullmatch(
            r"(?:tile|snap)\s+(.+?)\s+(?:to|on|onto)\s+(?:the\s+)?(left|right|top|bottom)(?:\s+(?:half|side))?(?:\s+of\s+(?:the\s+)?(?:screen|monitor|desktop))?[.!?]?$",
            request,
            re.IGNORECASE,
        )
        half = re.fullmatch(
            r"(?:put|move)\s+(.+?)\s+(?:to|on|onto)\s+(?:the\s+)?(left|right|top|bottom)\s+(?:half|side)(?:\s+of\s+(?:the\s+)?(?:screen|monitor|desktop))?[.!?]?$",
            request,
            re.IGNORECASE,
        )
        corner = re.fullmatch(
            r"(?:put|move|snap|tile)\s+(.+?)\s+(?:to|on|onto|in)\s+(?:(?:the|my)\s+)?(top|bottom)[ -](left|right)(?:\s+(?:corner|quarter))?(?:\s+(?:of\s+)?(?:(?:the|my)\s+)?(?:screen|monitor|desktop))?[.!?]?$",
            request,
            re.IGNORECASE,
        )
        if centered or tiled or half or corner:
            if centered:
                target, layout = self._resolve_window_pronoun(centered.group(1)), "center"
            elif corner:
                target = self._resolve_window_pronoun(corner.group(1))
                layout = f"{corner.group(2).lower()}-{corner.group(3).lower()}"
            else:
                match = tiled or half
                assert match is not None
                target, layout = (
                    self._resolve_window_pronoun(match.group(1)),
                    match.group(2).casefold(),
                )
            steps = [
                self._step(
                    "window",
                    "desktop.window.resolve",
                    {"description": target},
                    "One exact target window",
                ),
                self._step(
                    "layout",
                    "desktop.window.layout",
                    {"window_id": {"$ref": "window.result.window.id"}, "layout": layout},
                    "Window geometry matches the requested panel-aware layout",
                    ["window"],
                ),
            ]
            return TaskPlan(
                uuid.uuid4().hex,
                correlation_id,
                original,
                f"Place {target} in the {layout} layout",
                steps,
                dry_run,
            )

        workspace_move = re.fullmatch(
            r"(?:move|put|send)\s+(.+?)\s+(?:to|on|onto)\s+((?:virtual\s+)?(?:desktop|workspace)\s+.+?)[.!?]?$",
            request,
            re.IGNORECASE,
        )
        if workspace_move:
            target, workspace = self._resolve_window_pronoun(
                workspace_move.group(1)
            ), _clean_entity(workspace_move.group(2))
            steps = [
                self._step(
                    "window",
                    "desktop.window.resolve",
                    {"description": target},
                    "One exact target window",
                ),
                self._step(
                    "workspace",
                    "desktop.workspace.resolve",
                    {"description": workspace},
                    "One existing virtual desktop",
                ),
                self._step(
                    "move",
                    "desktop.window.move_to_workspace",
                    {
                        "window_id": {"$ref": "window.result.window.id"},
                        "desktop_id": {"$ref": "workspace.result.desktop.id"},
                    },
                    "Window is assigned to the requested virtual desktop",
                    ["window", "workspace"],
                ),
            ]
            return TaskPlan(
                uuid.uuid4().hex,
                correlation_id,
                original,
                f"Move {target} to {workspace}",
                steps,
                dry_run,
            )

        move = re.fullmatch(
            r"(?:move|put|throw)\s+(.+?)\s+(?:to|on|onto|over\s+to)\s+(.+?)[.!?]?$",
            request,
            re.IGNORECASE,
        )
        if move and re.search(
            r"\b(?:monitor|screen|display|other|left|right|laptop|external|main|second)\b",
            move.group(2),
            re.IGNORECASE,
        ):
            target, output = self._resolve_window_pronoun(move.group(1)), _clean_entity(
                move.group(2)
            )
            steps = [
                self._step(
                    "window",
                    "desktop.window.resolve",
                    {"description": target},
                    "One exact target window",
                ),
                self._step(
                    "output",
                    "desktop.output.resolve",
                    {"description": output},
                    "One enabled target output",
                ),
                self._step(
                    "move",
                    "desktop.window.move_to_output",
                    {
                        "window_id": {"$ref": "window.result.window.id"},
                        "output": {"$ref": "output.result.output.name"},
                    },
                    "Window center is on requested output",
                    ["window", "output"],
                ),
            ]
            return TaskPlan(
                uuid.uuid4().hex,
                correlation_id,
                original,
                f"Move {target} to {output}",
                steps,
                dry_run,
            )

        opening = re.fullmatch(
            r"(?:open|launch|start)\s+(.+?)\s+(?:on|in)\s+(.+?)[.!?]?$", request, re.IGNORECASE
        )
        if opening and re.search(
            r"\b(?:monitor|screen|display|other|left|right|laptop|external|main|second)\b",
            opening.group(2),
            re.IGNORECASE,
        ):
            application, output = _clean_entity(opening.group(1)), _clean_entity(opening.group(2))
            steps = [
                self._step(
                    "application",
                    "applications.list",
                    {"query": application, "limit": 8, "launch_only": True},
                    "One high-confidence desktop entry",
                ),
                self._step(
                    "launch",
                    "applications.open",
                    {"desktop_id": {"$ref": "application.result.applications.0.desktop_id"}},
                    "Application launch request succeeds",
                    ["application"],
                ),
                self._step(
                    "window",
                    "desktop.window.wait",
                    {"description": application, "timeout_seconds": 8},
                    "Application exposes a KWin window",
                    ["launch"],
                ),
                self._step(
                    "output",
                    "desktop.output.resolve",
                    {"description": output},
                    "One enabled target output",
                ),
                self._step(
                    "move",
                    "desktop.window.move_to_output",
                    {
                        "window_id": {"$ref": "window.result.window.id"},
                        "output": {"$ref": "output.result.output.name"},
                    },
                    "Window appears on requested output",
                    ["window", "output"],
                ),
            ]
            return TaskPlan(
                uuid.uuid4().hex,
                correlation_id,
                original,
                f"Open {application} on {output}",
                steps,
                dry_run,
            )

        opening = re.fullmatch(r"(?:open|launch|start)\s+(.+)", request, re.I)
        if opening and not re.search(
            r"\b(?:then|and|search|type|press)\b|[/\\:\"`“”]", opening[1], re.I
        ):
            from .intents import application_query

            target = application_query(opening[1])
            steps = [
                self._step(
                    "application",
                    "applications.list",
                    {"query": target, "limit": 8, "launch_only": True},
                    "One unambiguous application",
                ),
                self._step(
                    "launch",
                    "applications.open",
                    {"desktop_id": {"$ref": "application.result.applications.0.desktop_id"}},
                    "Launch request accepted",
                    ["application"],
                ),
            ]
            return TaskPlan(
                uuid.uuid4().hex, correlation_id, original, f"Open {target}", steps, dry_run
            )
        return None

    @staticmethod
    def _lookup(results: dict[str, dict[str, Any]], path: str) -> Any:
        head, *tail = path.split(".")
        value: Any = results[head]
        for part in tail:
            if isinstance(value, list):
                value = value[int(part)]
            else:
                value = value[part]
        return value

    def _resolve_arguments(self, value: Any, results: dict[str, dict[str, Any]]) -> Any:
        if isinstance(value, dict) and set(value) == {"$ref"}:
            return self._lookup(results, str(value["$ref"]))
        if isinstance(value, dict):
            return {key: self._resolve_arguments(item, results) for key, item in value.items()}
        if isinstance(value, list):
            return [self._resolve_arguments(item, results) for item in value]
        return value

    @staticmethod
    def _verified(step: PlanStep, result: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        payload = result.get("result", {}) if isinstance(result.get("result"), dict) else {}
        if result.get("status") != "completed":
            return False, {
                "verified": False,
                "reason": payload.get("error")
                or result.get("response")
                or result.get("status", "unknown"),
                "evidence": payload,
            }
        if payload.get("ok") is False or payload.get("error") or payload.get("truncated"):
            return False, {
                "verified": False,
                "reason": str(payload.get("error") or "Tool failed or its result was truncated"),
                "evidence": payload,
            }
        if step.tool == "system.clock" and not (
            payload.get("local_time") and payload.get("local_date")
        ):
            return False, {
                "verified": False,
                "reason": "The system clock did not return time/date evidence",
            }
        if (
            step.tool
            in {
                "notes.list",
                "tasks.list",
                "bookmarks.list",
                "snippets.list",
                "files.recent",
                "desktop.controls.list",
            }
            and "items" not in payload
        ):
            return False, {
                "verified": False,
                "reason": "The list was unavailable or exceeded the output limit. Narrow the search before selecting an item.",
            }
        if step.tool == "applications.list":
            applications = payload.get("applications", [])
            unique = len(applications) == 1 or (
                applications
                and (
                    len(applications) < 2
                    or applications[0].get("match_score", 0) > applications[1].get("match_score", 0)
                )
            )
            return bool(unique), {"verified": bool(unique), "matches": len(applications)}
        execution = result.get("execution") or evaluate_result(step.tool, payload).public()
        return execution["ok"], {**execution, "reason": execution.get("error"), "evidence": payload}

    @staticmethod
    def _retryable(step: PlanStep) -> bool:
        # Opt in known idempotent primitives. A new SAFE tool may still mutate
        # state and must not acquire automatic replay by default.
        return step.permission_class in {"SAFE", "LOW_RISK"} and step.tool in (
            OBSERVATION_TOOLS
            | {
                "desktop.window.activate",
                "desktop.window.state",
                "desktop.window.move_resize",
                "desktop.window.move_to_output",
                "desktop.window.move_to_workspace",
                "desktop.window.layout",
                "audio.set_volume",
                "audio.set_mute",
            }
        )

    async def execute(
        self,
        plan: TaskPlan,
        start_index: int = 0,
        prior_results: dict[str, dict[str, Any]] | None = None,
        *,
        requester: ToolRequester | None = None,
        own_confirmation: bool = True,
    ) -> dict[str, Any]:
        request_tool = requester or self.request_tool
        async with self._lock:
            self.active = plan
            results = dict(prior_results or {})
            plan.status = "DRY_RUN" if plan.dry_run else "RUNNING"
            self.bus.publish("plan.created", "planner", plan.public(), plan.correlation_id)
            if plan.dry_run:
                plan.duration_ms = round((time.monotonic() - plan.created_monotonic) * 1000, 3)
                self._archive(plan)
                self._settle_terminal_state("Dry-run plan completed", plan.correlation_id)
                return self._response(plan, "Here's the plan. I didn't run it.")
            for index in range(start_index, len(plan.steps)):
                plan.current_step = index
                step = plan.steps[index]
                if plan.cancellation_reason:
                    step.status = StepStatus.CANCELLED
                    plan.status = "CANCELLED"
                    self.bus.publish(
                        "plan.cancelled",
                        "planner",
                        {"plan_id": plan.id, "reason": plan.cancellation_reason},
                        plan.correlation_id,
                    )
                    self._archive(plan)
                    self._settle_terminal_state("Plan cancelled", plan.correlation_id)
                    return self._response(plan, "Stopped.")
                try:
                    arguments = self._resolve_arguments(step.arguments, results)
                    step.resolved_arguments = arguments
                except (KeyError, IndexError, TypeError, ValueError) as error:
                    return self._fail(
                        plan, step, f"A required earlier result was unavailable: {error}"
                    )
                step.status = StepStatus.RUNNING
                step.attempts += 1
                started = time.perf_counter()
                self.state.transition(
                    CoreState.USING_TOOL,
                    f"{step.tool} ({index + 1}/{len(plan.steps)})",
                    plan.correlation_id,
                    {"plan_id": plan.id, "step": index + 1, "steps": len(plan.steps)},
                )
                self.bus.publish(
                    "plan.step_started",
                    "planner",
                    {
                        "plan_id": plan.id,
                        "step": asdict(step),
                        "position": index + 1,
                        "total": len(plan.steps),
                    },
                    plan.correlation_id,
                )
                try:
                    if step.tool in {
                        "notes.list",
                        "tasks.list",
                        "bookmarks.list",
                        "snippets.list",
                        "files.recent",
                        "desktop.controls.list",
                    }:
                        self.last_entities.pop("choices", None)
                    result = await request_tool(
                        {
                            "name": step.tool,
                            "arguments": arguments,
                            "correlation_id": plan.correlation_id,
                        },
                        plan.correlation_id,
                    )
                except Exception as error:
                    if plan.cancellation_reason:
                        step.duration_ms = round((time.perf_counter() - started) * 1000, 3)
                        step.status = StepStatus.CANCELLED
                        step.error = plan.cancellation_reason
                        plan.status = "CANCELLED"
                        plan.duration_ms = round(
                            (time.monotonic() - plan.created_monotonic) * 1000, 3
                        )
                        plan.timings[step.id] = step.duration_ms
                        plan.timings["total"] = plan.duration_ms
                        self.bus.publish(
                            "plan.cancelled",
                            "planner",
                            {"plan_id": plan.id, "reason": plan.cancellation_reason},
                            plan.correlation_id,
                            plan.duration_ms,
                        )
                        self._archive(plan)
                        self._settle_terminal_state("Plan cancelled", plan.correlation_id)
                        return self._response(plan, "Stopped.")
                    # One bounded retry is allowed only for non-destructive,
                    # idempotent observation/low-risk actions.
                    if self._retryable(step) and step.attempts < 2:
                        step.attempts += 1
                        recovery = {
                            "step": step.id,
                            "strategy": "bounded_retry",
                            "trigger": str(error),
                            "attempt": step.attempts,
                            "outcome": "RUNNING",
                        }
                        plan.recovery.append(recovery)
                        await asyncio.sleep(0.15)
                        try:
                            result = await request_tool(
                                {
                                    "name": step.tool,
                                    "arguments": arguments,
                                    "correlation_id": plan.correlation_id,
                                },
                                plan.correlation_id,
                            )
                            recovery["outcome"] = "SUCCEEDED"
                        except Exception as retry_error:
                            recovery["outcome"] = "FAILED"
                            recovery["error"] = str(retry_error)
                            step.duration_ms = round((time.perf_counter() - started) * 1000, 3)
                            return self._fail(plan, step, str(retry_error))
                    else:
                        step.duration_ms = round((time.perf_counter() - started) * 1000, 3)
                        return self._fail(plan, step, str(error))
                step.duration_ms = round((time.perf_counter() - started) * 1000, 3)
                if plan.cancellation_reason:
                    step.actual_result = result
                    step.status = StepStatus.CANCELLED
                    plan.status = "CANCELLED"
                    plan.duration_ms = round((time.monotonic() - plan.created_monotonic) * 1000, 3)
                    self._archive(plan)
                    self._settle_terminal_state("Plan cancelled", plan.correlation_id)
                    return self._response(
                        plan, "Stopped. An action already sent may still have taken effect."
                    )
                if result.get("status") == "confirmation_required":
                    step.status = StepStatus.WAITING_CONFIRMATION
                    step.actual_result = result
                    plan.status = "WAITING_CONFIRMATION"
                    confirmation_id = str(result["confirmation"]["id"])
                    if own_confirmation:
                        self.pending[confirmation_id] = (plan, index)
                    else:
                        # CommandEngine owns provider continuation after consent.
                        self.external_pending[confirmation_id] = (plan, index)
                        self._archive(plan)
                    return {
                        **self._response(plan, f"I need confirmation before {step.tool}."),
                        "status": "confirmation_required",
                        "confirmation": result["confirmation"],
                    }
                step.actual_result = result
                ok, verification = self._verified(step, result)
                step.verification = verification
                if not ok and self._retryable(step) and step.attempts < 2:
                    step.attempts += 1
                    recovery = {
                        "step": step.id,
                        "strategy": "refresh_and_verify_once",
                        "trigger": str(verification.get("reason", "postcondition_not_met")),
                        "attempt": step.attempts,
                        "outcome": "RUNNING",
                    }
                    plan.recovery.append(recovery)
                    self.bus.publish(
                        "plan.recovering",
                        "planner",
                        {"plan_id": plan.id, "step_id": step.id},
                        plan.correlation_id,
                    )
                    await asyncio.sleep(0.18)
                    try:
                        result = await request_tool(
                            {
                                "name": step.tool,
                                "arguments": arguments,
                                "correlation_id": plan.correlation_id,
                            },
                            plan.correlation_id,
                        )
                    except Exception as retry_error:
                        recovery["outcome"] = "FAILED"
                        recovery["error"] = str(retry_error)
                        step.duration_ms = round((time.perf_counter() - started) * 1000, 3)
                        return self._fail(plan, step, str(retry_error))
                    step.actual_result = result
                    ok, verification = self._verified(step, result)
                    step.verification = verification
                    recovery["outcome"] = "SUCCEEDED" if ok else "FAILED"
                step.duration_ms = round((time.perf_counter() - started) * 1000, 3)
                if not ok:
                    evidence = verification.get("evidence", {})
                    detail = (
                        (evidence.get("error") or evidence.get("message"))
                        if isinstance(evidence, dict)
                        else None
                    )
                    return self._fail(
                        plan,
                        step,
                        str(
                            detail
                            or verification.get("reason")
                            or f"Verification failed for {step.expected_postcondition}"
                        ),
                    )
                step.status = StepStatus.SUCCEEDED
                plan.timings[step.id] = step.duration_ms
                results[step.id] = result
                payload = result.get("result", {}) if isinstance(result.get("result"), dict) else {}
                if payload.get("choice_kind") in {
                    "note",
                    "task",
                    "bookmark",
                    "snippet",
                    "file",
                    "control",
                } and isinstance(payload.get("items"), list):
                    self.last_entities["choices"] = {
                        "kind": payload["choice_kind"],
                        "items": payload["items"][:100],
                        "at": time.monotonic(),
                    }
                if isinstance(payload.get("window"), dict):
                    self.last_entities["window"] = dict(payload["window"])
                    self.last_entities["window_at"] = time.monotonic()
                if isinstance(payload.get("output"), dict):
                    self.last_entities["output"] = dict(payload["output"])
                if step.tool in {
                    "desktop.window.state",
                    "desktop.window.layout",
                    "desktop.window.move_resize",
                    "desktop.window.move_to_output",
                    "desktop.window.move_to_workspace",
                }:
                    self.last_window_operation = {
                        "tool": step.tool,
                        "arguments": dict(step.resolved_arguments or {}),
                    }
                if step.permission_class == "SAFE":
                    plan.world_state_used[step.id] = result.get("result", {})
                self.bus.publish(
                    "plan.step_completed",
                    "planner",
                    {"plan_id": plan.id, "step": asdict(step)},
                    plan.correlation_id,
                    step.duration_ms,
                )
            if plan.goal_conditions:
                from .goals import verify_conditions

                self.bus.publish(
                    "plan.goal_verifying", "planner", {"plan_id": plan.id}, plan.correlation_id
                )
                conditions = self._resolve_arguments(plan.goal_conditions, results)
                plan.goal_verification = await verify_conditions(
                    conditions,
                    request_tool,
                    plan.correlation_id,
                    cancelled=lambda: bool(plan.cancellation_reason),
                )
                self.bus.publish(
                    "plan.goal_verified", "planner", plan.goal_verification, plan.correlation_id
                )
                if not plan.goal_verification["verified"]:
                    return self._fail(
                        plan,
                        plan.steps[-1],
                        "Actions finished, but the declared goal conditions were not satisfied",
                    )
            plan.status = "SUCCEEDED"
            plan.duration_ms = round((time.monotonic() - plan.created_monotonic) * 1000, 3)
            plan.timings["total"] = plan.duration_ms
            self.bus.publish(
                "plan.completed", "planner", plan.public(), plan.correlation_id, plan.duration_ms
            )
            self._archive(plan)
            self._settle_terminal_state("Plan completed", plan.correlation_id)
            unverified_input = any(
                item.verification and not item.verification.get("verified") for item in plan.steps
            )
            response = (
                "Sent the requested input. The application's resulting state has not been verified."
                if unverified_input
                else "Done."
            )
            if plan.steps and plan.steps[-1].tool == "applications.open" and unverified_input:
                response = (
                    "The application launch request was accepted; its window has not been verified."
                )
            if plan.steps and plan.steps[-1].tool in {
                "browser.open_url",
                "bookmarks.open",
                "settings.open",
                "files.open_selected",
            }:
                response = str(
                    (plan.steps[-1].actual_result or {}).get("result", {}).get("message")
                    or response
                )
            if not unverified_input and plan.steps:
                final_payload = (plan.steps[-1].actual_result or {}).get("result", {})
                if plan.steps[-1].tool == "system.clock":
                    response = (
                        f"Today is {final_payload['local_date']}."
                        if clock_request(plan.request) == "date"
                        else f"It's {final_payload['local_time']} {final_payload.get('timezone', '')}.".replace(
                            " .", "."
                        )
                    )
                elif isinstance(final_payload, dict) and final_payload.get("message"):
                    response = str(final_payload["message"])
                elif isinstance(final_payload, dict) and "reminders" in final_payload:
                    response = (
                        "Pending reminders: "
                        + ", ".join(item["label"] for item in final_payload["reminders"])
                        if final_payload["reminders"]
                        else "No pending reminders."
                    )
                elif isinstance(final_payload, dict) and "items" in final_payload:
                    response = (
                        "Saved items: " + ", ".join(final_payload["items"])
                        if final_payload["items"]
                        else "No saved items yet."
                    )
            return self._response(plan, response)

    def record_external_confirmation(
        self, confirmation_id: str, tool_result: dict[str, Any]
    ) -> dict[str, Any]:
        pending = self.external_pending.pop(confirmation_id, None)
        if pending is None:
            return tool_result
        plan, index = pending
        step = plan.steps[index]
        step.actual_result = tool_result
        ok, step.verification = self._verified(step, tool_result)
        step.status = StepStatus.SUCCEEDED if ok else StepStatus.FAILED
        plan.status = "SUCCEEDED" if ok else "FAILED"
        plan.duration_ms = round((time.monotonic() - plan.created_monotonic) * 1000, 3)
        self.bus.publish(
            "plan.completed" if ok else "plan.failed", "planner", plan.public(), plan.correlation_id
        )
        return {
            **tool_result,
            "status": (
                "completed"
                if ok
                else (
                    "failed"
                    if tool_result.get("status") == "completed"
                    else tool_result.get("status", "failed")
                )
            ),
            "verification": step.verification,
            "plan_id": plan.id,
        }

    async def resume_confirmation(
        self, confirmation_id: str, tool_result: dict[str, Any]
    ) -> dict[str, Any] | None:
        pending = self.pending.pop(confirmation_id, None)
        if pending is None:
            return None
        plan, index = pending
        step = plan.steps[index]
        step.actual_result = tool_result
        ok, verification = self._verified(step, tool_result)
        step.verification = verification
        if not ok:
            return self._fail(plan, step, "The confirmed action did not verify")
        step.status = StepStatus.SUCCEEDED
        results = {
            item.id: item.actual_result
            for item in plan.steps[: index + 1]
            if item.actual_result is not None
        }
        return await self.execute(plan, index + 1, results)

    def reject_confirmation(
        self, confirmation_id: str, reason: str = "permission_denied"
    ) -> dict[str, Any] | None:
        pending = self.pending.pop(confirmation_id, None)
        if pending is None:
            return None
        plan, index = pending
        step = plan.steps[index]
        step.status = StepStatus.CANCELLED
        step.error = reason
        step.verification = {"verified": False, "reason": reason}
        plan.status = "DENIED"
        plan.cancellation_reason = reason
        plan.duration_ms = round((time.monotonic() - plan.created_monotonic) * 1000, 3)
        self.bus.publish(
            "plan.cancelled",
            "planner",
            {"plan_id": plan.id, "reason": reason, "confirmation_id": confirmation_id},
            plan.correlation_id,
            plan.duration_ms,
        )
        self._archive(plan)
        return self._response(plan, "I didn't run that action.")

    def cancel(self, reason: str = "user_request") -> dict[str, Any]:
        if self.active is None or self.active.status not in {"RUNNING", "WAITING_CONFIRMATION"}:
            return {"status": "no_active_plan"}
        self.active.cancellation_reason = reason
        self.active.status = "CANCEL_REQUESTED"
        if self.active.current_step < len(self.active.steps):
            self.active.steps[self.active.current_step].status = StepStatus.CANCEL_REQUESTED
        self.bus.publish(
            "plan.cancel_requested",
            "planner",
            {"plan_id": self.active.id, "reason": reason},
            self.active.correlation_id,
        )
        confirmation_ids = [
            confirmation_id
            for confirmation_id, (plan, _index) in self.pending.items()
            if plan is self.active
        ]
        if confirmation_ids:
            plan = self.active
            for confirmation_id in confirmation_ids:
                self.pending.pop(confirmation_id, None)
            if plan.current_step < len(plan.steps):
                plan.steps[plan.current_step].status = StepStatus.CANCELLED
                plan.steps[plan.current_step].error = reason
            plan.status = "CANCELLED"
            plan.duration_ms = round((time.monotonic() - plan.created_monotonic) * 1000, 3)
            self.bus.publish(
                "plan.cancelled",
                "planner",
                {"plan_id": plan.id, "reason": reason},
                plan.correlation_id,
                plan.duration_ms,
            )
            self._archive(plan)
            return {
                "status": "cancelled",
                "plan_id": plan.id,
                "correlation_id": plan.correlation_id,
                "confirmation_ids": confirmation_ids,
            }
        return {
            "status": "cancel_requested",
            "plan_id": self.active.id,
            "correlation_id": self.active.correlation_id,
            "confirmation_ids": [],
        }

    def _fail(self, plan: TaskPlan, step: PlanStep, error: str) -> dict[str, Any]:
        step.status = StepStatus.FAILED
        step.error = error
        plan.status = "FAILED"
        plan.duration_ms = round((time.monotonic() - plan.created_monotonic) * 1000, 3)
        if step.duration_ms is not None:
            plan.timings[step.id] = step.duration_ms
        plan.timings["total"] = plan.duration_ms
        if step.permission_class == "SAFE" and isinstance(step.actual_result, dict):
            observed = step.actual_result.get("result")
            if isinstance(observed, dict):
                plan.world_state_used.setdefault(step.id, observed)
        lowered = error.casefold()
        if (
            "ambiguous" in lowered
            or "multiple" in lowered
            or ("no " in lowered and " match" in lowered)
        ):
            gap_type = "AMBIGUOUS_REQUEST"
        elif "permission" in lowered or "denied" in lowered:
            gap_type = "MISSING_PERMISSION"
        elif "provider" in lowered or "model" in lowered:
            gap_type = "MISSING_PROVIDER"
        elif "unavailable" in lowered or "not supported" in lowered:
            gap_type = "MISSING_DESKTOP_BACKEND"
        else:
            gap_type = "BUG"
        gap = self.capability_gap(step.tool, error, gap_type)
        plan.capability_gaps.append(gap)
        plan.failure_report = {
            "request": plan.request,
            "resolved_goal": plan.goal,
            "failed_step": step.id,
            "tool": step.tool,
            "arguments": step.resolved_arguments or step.arguments,
            "expected_postcondition": step.expected_postcondition,
            "actual_result": step.actual_result,
            "verification": step.verification,
            "error": error,
            "attempts": step.attempts,
            "completed_steps": [
                item.id for item in plan.steps if item.status == StepStatus.SUCCEEDED
            ],
            "world_state_used": plan.world_state_used,
            "recovery": plan.recovery,
            "capability_gap": gap,
            "duration_ms": plan.duration_ms,
        }
        self.bus.publish(
            "plan.failed",
            "planner",
            {"plan": plan.public(), "failure_report": plan.failure_report},
            plan.correlation_id,
            plan.duration_ms,
        )
        self._archive(plan)
        self._settle_terminal_state("Plan failed", plan.correlation_id)
        return self._response(plan, f"I couldn't finish that. {error}")

    def _archive(self, plan: TaskPlan) -> None:
        if plan not in self.recent:
            self.recent.append(plan)
            self.recent = self.recent[-24:]
        if self.active is plan:
            self.active = None

    @staticmethod
    def _response(plan: TaskPlan, response: str) -> dict[str, Any]:
        return {
            "goal_verified": bool(
                plan.goal_verification
                and plan.goal_verification.get("verified")
                and plan.goal_source == "trusted_intent"
            ),
            "declared_conditions_verified": bool(
                plan.goal_verification and plan.goal_verification.get("verified")
            ),
            "status": (
                "completed" if plan.status in {"SUCCEEDED", "DRY_RUN"} else plan.status.casefold()
            ),
            "correlation_id": plan.correlation_id,
            "response": response,
            "plan": plan.public(),
            "duration_ms": plan.duration_ms,
            "cognition": {
                "provider": "deterministic-planner",
                "model": "ev-plan-v1",
                "interpreted_task": plan.goal,
                "plan": [step.expected_postcondition for step in plan.steps],
                "usage": {},
                "latency_ms": plan.timings.get("planning", 0.0),
                "response_id": "",
            },
        }
