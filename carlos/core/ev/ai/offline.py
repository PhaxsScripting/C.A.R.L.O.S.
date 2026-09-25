from __future__ import annotations

import re
import time
import uuid
from pathlib import Path
from typing import Any

from ..intents import CLOSE_APPLICATION_PATTERN, application_query
from ..commands import request_text, clock_request, audio_request, website_request, media_request
from ..voice.normalization import is_negative_action, normalize_transcript
from .base import Provider, ProviderTurn, ToolCall


def call(name: str, arguments: dict[str, Any] | None = None) -> ToolCall:
    return ToolCall(uuid.uuid4().hex, name, arguments or {})


_OPEN_APPLICATION = re.compile(
    r"\b(?:open|launch|start)\s+"
    r"(?:(?:a|an|the)\s+)?(?:(?:new|another|up)\s+)?"
    r"(?:(?:copy|instance|window|thing)(?:\s+of)?\s+)?"
    r"(.+?)(?:\s+app(?:lication)?)?[.!?]?\s*$",
    re.IGNORECASE,
)
_CLOSE_APPLICATION = CLOSE_APPLICATION_PATTERN


def _open_application_name(text: str) -> str | None:
    """Extract only the app noun from a possibly compound spoken request."""

    match = _OPEN_APPLICATION.search(text)
    if match is None:
        return None
    candidate = re.split(
        r"\s*(?:[,;]\s*|\s+(?:and\s+)?then\s+|\s+and\s+)(?=(?:open|focus|switch|type|press|click|search|look\s+up|find)\b)",
        match.group(1),
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return application_query(candidate)


_LITERAL = r"""(?:"[^"\n]*"|'[^'\n]*'|“[^”\n]*”|‘[^’\n]*’)"""
_INPUT_VERB = r"(?:focus|switch\s+to|bring|type|write|enter|press|hit|send|click|double[- ]click|right[- ]click|middle[- ]click|scroll|move\s+(?:the\s+)?(?:mouse|pointer|cursor))\b"
_INPUT_SEPARATOR = re.compile(
    rf"(?:\s*[,;]\s*|\s+(?:and\s+)?then\s+|\s+and\s+)(?={_INPUT_VERB})", re.IGNORECASE
)
_INPUT_KEYS = {
    "return": "enter",
    "esc": "escape",
    "spacebar": "space",
    "page up": "pageup",
    "page down": "pagedown",
    "back space": "backspace",
    "delete": "delete",
    "up arrow": "up",
    "down arrow": "down",
    "left arrow": "left",
    "right arrow": "right",
}
_NAMED_KEYS = {
    "enter",
    "escape",
    "tab",
    "backspace",
    "delete",
    "space",
    "home",
    "end",
    "left",
    "right",
    "up",
    "down",
    "pageup",
    "pagedown",
}


def _desktop_command_text(text: str) -> str:
    # Only strip an invocation prefix. Voice cleanup must never rewrite text
    # which the user has explicitly asked us to type.
    text = re.sub(
        r"^\s*(?:hey\s+)?(?:e\.?\s*v\.?|eve|evie)\b[\s,;:!.-]*", "", text, flags=re.IGNORECASE
    )
    return re.sub(
        r"^\s*(?:(?:please|can you|could you|would you)\s+)+", "", text.strip(), flags=re.IGNORECASE
    )


def mask_desktop_literals(text: str) -> str:
    # Use non-whitespace placeholders: a separator's \s+ must not swallow a
    # quoted payload while matching the command that follows it.
    return re.sub(_LITERAL, lambda match: "\ufffc" * len(match.group()), text)


def is_desktop_input_request(text: str) -> bool:
    request = _desktop_command_text(text)
    prefixed = re.match(r"^(?:in|on)\s+.+?,\s*(.+)$", request, re.IGNORECASE)
    action = prefixed.group(1) if prefixed else request
    if re.match(r"^focus\s+on\b", action, re.IGNORECASE):
        return False
    if re.match(r"^(?:write|enter)\s+", action, re.IGNORECASE):
        # These words are common conversational verbs ("write me a poem",
        # "enter a contest"). Require an explicitly quoted typing payload.
        return bool(re.match(rf"^(?:write|enter)\s+{_LITERAL}", action, re.IGNORECASE))
    if re.match(r"^type\s+of\b", action, re.IGNORECASE):
        return False
    if re.match(
        r"^switch\s+to\s+(?:(?:a|an|the|another|different|new)\s+)*(?:topic|subject|idea|approach|discussion|conversation|question)\b",
        action,
        re.IGNORECASE,
    ):
        return False
    if re.match(
        r"^bring\s+up\s+(?:(?:a|an|the|another|different|new|next|previous|following)\s+)*(?:topic|subject|idea|issue|point|question|summary)\b",
        action,
        re.IGNORECASE,
    ):
        return False
    if re.match(
        r"^(?:focus\s+(?!on\b)|switch\s+to\s+|bring\s+up\s+|bring\s+.+?\s+to\s+(?:the\s+)?(?:front|foreground)\b)",
        action,
        re.IGNORECASE,
    ):
        return True
    if re.match(r"^type\s+", action, re.IGNORECASE):
        return True
    key = re.match(r"^(?:press|hit|send)\s+(.+)", action, re.IGNORECASE)
    if key:
        key_text = re.sub(r"\s+(?:in|on)\s+.+$", "", key.group(1), flags=re.IGNORECASE)
        return _input_key(key_text) is not None
    return bool(
        re.match(
            r"^(?:click|double[- ]click|right[- ]click|middle[- ]click|scroll\b|move\s+(?:the\s+)?(?:mouse|pointer|cursor)\b)",
            action,
            re.IGNORECASE,
        )
    )


def _desktop_entity(text: str) -> str:
    text = re.sub(r"^(?:(?:the|my)\s+)+", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\s+(?:app|application|window)$", "", text, flags=re.IGNORECASE).strip(' .!?"“”')
    if text.casefold() in {"current", "active", "focused", "this", "it", "there"}:
        return "current window"
    return text


def _input_key(text: str) -> dict[str, Any] | None:
    text = re.sub(r"\s+(?:key|keys|shortcut)$", "", text.strip(), flags=re.IGNORECASE).casefold()
    text = re.sub(r"\s+(?:plus|and)\s+|\s*\+\s*|\s*-\s*", "+", text)
    text = re.sub(r"^(control|ctrl|shift|alt|super|meta)\s+", r"\1+", text)
    parts = text.split("+")
    modifiers = [{"control": "ctrl", "meta": "super"}.get(part, part) for part in parts[:-1]]
    if len(modifiers) > 4 or any(
        part not in {"ctrl", "shift", "alt", "super"} for part in modifiers
    ):
        return None
    key = _INPUT_KEYS.get(parts[-1], parts[-1])
    if (
        key not in _NAMED_KEYS
        and not re.fullmatch(r"f(?:[1-9]|1[0-2])", key)
        and not (len(key) == 1 and key.isprintable())
    ):
        return None
    return {"key": key, "modifiers": list(dict.fromkeys(modifiers))}


def parse_desktop_input(text: str) -> dict[str, Any] | None:
    """Parse explicit desktop actions, never instructions from observed content.

    Returns only fixed tool names and user-specified data. Quoted payloads are
    opaque, so words such as 'and click', 'close', or 'CPU' cannot become actions.
    Unresolved names and focus are checked later against the live desktop.
    """
    request = _desktop_command_text(text)
    window = "current window"
    prefix = re.match(rf"^(?:in|on)\s+(.+?),\s*(?={_INPUT_VERB})", request, re.IGNORECASE)
    if prefix:
        window, request = _desktop_entity(prefix.group(1)), request[prefix.end() :]
    masked = mask_desktop_literals(request)
    bounds = list(_INPUT_SEPARATOR.finditer(masked))
    starts = [0, *(match.end() for match in bounds)]
    ends = [*(match.start() for match in bounds), len(request)]
    if len(starts) > 8:
        return None
    actions: list[dict[str, Any]] = []
    for start, end in zip(starts, ends):
        raw_clause = request[start:end].strip()
        clause = raw_clause.rstrip(".!?")
        focus = re.fullmatch(
            r"(?:focus|switch\s+to|bring\s+up)\s+(.+?)|bring\s+(.+?)\s+to\s+(?:the\s+)?(?:front|foreground)",
            clause,
            re.IGNORECASE,
        )
        if focus:
            if not is_desktop_input_request(raw_clause):
                return None
            window = _desktop_entity(focus.group(1) or focus.group(2))
            if not window:
                return None
            actions.append({"tool": "desktop.window.activate", "window": window, "arguments": {}})
            continue

        typing = re.match(r"^(?:type|write|enter)\s+", raw_clause, re.IGNORECASE)
        if typing:
            if not is_desktop_input_request(raw_clause):
                return None
            body = raw_clause[typing.end() :]
            quoted = re.match(_LITERAL, body)
            field = ""
            if quoted:
                payload, tail = quoted.group()[1:-1], body[quoted.end() :].strip()
                named = re.fullmatch(
                    rf"(?:in(?:to)?|to)\s+(?:the\s+)?({_LITERAL}|.+?)\s+(?:field|box)(?:\s+(?:in|on)\s+(.+))?",
                    tail,
                    re.IGNORECASE,
                )
                if named:
                    field = named.group(1).strip("\"'“”‘’")
                    if named.group(2):
                        window = _desktop_entity(named.group(2))
                elif tail:
                    target = re.fullmatch(r"(?:in(?:to)?|on)\s+(.+)", tail, re.IGNORECASE)
                    if target is None:
                        return None
                    window = _desktop_entity(target.group(1))
            else:
                # The remainder of an unquoted type command is literal text,
                # apart from the strong "into <target>" form or a target
                # explicitly labelled as a window/app. Plain "in" can be
                # literal content, as in "type meet me in five".
                target = re.fullmatch(
                    r"(.+?)\s+(?:into\s+(.+)|(?:in|on)\s+(?:the\s+)?(.+?)\s+(?:window|app|application))",
                    body,
                    re.IGNORECASE,
                )
                payload = target.group(1) if target else body
                if target:
                    window = _desktop_entity(target.group(2) or target.group(3))
            if (
                not payload
                or len(payload) > (10000 if field else 2000)
                or any(not char.isprintable() for char in payload)
            ):
                return None
            if not window:
                return None
            arguments = (
                {"application": window, "name": field, "text": payload}
                if field
                else {"text": payload}
            )
            actions.append(
                {
                    "tool": (
                        "accessibility.element.set_text" if field else "desktop.keyboard.type_text"
                    ),
                    "window": window,
                    "arguments": arguments,
                }
            )
            continue

        # App targets are parsed only outside literal control names.
        masked_clause = mask_desktop_literals(clause)
        targets = list(re.finditer(r"\s+(?:in|on)\s+", masked_clause, re.IGNORECASE))
        target = targets[-1] if targets else None
        if target and re.fullmatch(r"click", clause[: target.start()], re.IGNORECASE):
            target = None
        if target:
            window = _desktop_entity(clause[target.end() :])
            clause = clause[: target.start()].strip()
        if not window:
            return None
        key = re.fullmatch(r"(?:press|hit|send)\s+(.+)", clause, re.IGNORECASE)
        if key:
            arguments = _input_key(key.group(1))
            if arguments is None:
                return None
            actions.append(
                {"tool": "desktop.keyboard.key", "window": window, "arguments": arguments}
            )
            continue
        point = r"\(?\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*\)?"
        click = re.fullmatch(
            rf"(click|double[- ]click|right[- ]click|middle[- ]click)(?:\s+(?:at|on))?\s+{point}",
            clause,
            re.IGNORECASE,
        )
        move = re.fullmatch(
            rf"move\s+(?:the\s+)?(?:mouse|pointer|cursor)\s+(?:to|at)\s+{point}",
            clause,
            re.IGNORECASE,
        )
        scroll = re.fullmatch(
            rf"scroll\s+(up|down|left|right)(?:\s+(\d+)\s*(?:steps?|notches?|clicks?)?)?(?:\s+(?:at|from)\s+{point})?",
            clause,
            re.IGNORECASE,
        )
        if click or move or scroll:
            if click:
                kind, x, y = click.groups()
                kind = kind.casefold()
                arguments = {
                    "x": float(x),
                    "y": float(y),
                    "button": (
                        "right"
                        if kind.startswith("right")
                        else "middle" if kind.startswith("middle") else "left"
                    ),
                    "count": 2 if kind.startswith("double") else 1,
                }
                tool = "desktop.pointer.click"
            elif move:
                arguments = {"x": float(move.group(1)), "y": float(move.group(2))}
                tool = "desktop.pointer.move"
            else:
                assert scroll is not None
                direction, steps, x, y = scroll.groups()
                arguments = {"direction": direction.casefold(), "steps": int(steps or 3)}
                if not 1 <= arguments["steps"] <= 20:
                    return None
                if x is not None:
                    arguments.update(x=float(x), y=float(y))
                tool = "desktop.pointer.scroll"
            if any(abs(arguments[axis]) > 32768 for axis in ("x", "y") if axis in arguments):
                return None
            actions.append({"tool": tool, "window": window, "arguments": arguments})
            continue
        semantic = re.fullmatch(
            rf"click\s+(?:on\s+)?(?:the\s+)?({_LITERAL}|.+?)(?:\s+(button|link|menu\s+item|tab|checkbox))?",
            clause,
            re.IGNORECASE,
        )
        if semantic:
            name = semantic.group(1).strip("\"'“”‘’")
            if name.casefold() in {"it", "that", "there", "this"} or re.search(
                r"\d\s*,\s*\d|\b(?:then|and click)\b", name, re.IGNORECASE
            ):
                return None
            arguments = {"application": window, "name": name}
            if semantic.group(2):
                arguments["role"] = semantic.group(2).casefold()
            actions.append(
                {"tool": "accessibility.element.activate", "window": window, "arguments": arguments}
            )
            continue
        return None
    if not actions:
        return None
    return {"actions": actions}


def _recent_subject(context: list[dict[str, Any]]) -> str:
    for item in reversed(context[-8:]):
        content = str(item.get("content", "")).casefold()
        if re.search(r"\b(ram|memory)\b", content):
            return "memory"
        if re.search(r"\b(cpu|processor)\b", content):
            return "cpu"
    return ""


def _recent_top_process(context: list[dict[str, Any]]) -> tuple[str, str] | None:
    for item in reversed(context[-6:]):
        if item.get("role") != "assistant":
            continue
        content = str(item.get("content", ""))
        match = re.search(
            r"Top (?:memory|CPU) users are ([^,.]+?) at ([\d.]+\s*(?:MB|GB|%))",
            content,
            re.IGNORECASE,
        )
        if match:
            return match.group(1).strip(), match.group(2).strip()
    return None


class OfflineProvider(Provider):
    """Small, explicitly limited fallback for useful commands without an API."""

    name = "offline"

    @property
    def available(self) -> tuple[bool, str]:
        return True, "Limited local command fallback"

    @property
    def model(self) -> str:
        return "ev-safe-local"

    async def begin(
        self,
        user_text: str,
        context: list[dict[str, Any]],
        memories: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ProviderTurn:
        started = time.perf_counter()
        text = normalize_transcript(user_text.strip())
        if requested_clock := clock_request(text):
            return ProviderTurn(
                self.name,
                self.model,
                "",
                tool_calls=[call("system.clock")],
                interpreted_task="Read the computer's current local clock directly.",
                continuation={"clock_display": requested_clock},
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        if re.fullmatch(
            r"what(?:'s|\s+is)\s+the\s+timer(?:\s+(?:at|now|right\s+now))?[.!?]*", text, re.I
        ):
            return ProviderTurn(
                self.name,
                self.model,
                "",
                tool_calls=[call("reminders.list")],
                interpreted_task="Read actual pending timers instead of inventing timer state.",
            )
        if is_negative_action(text):
            return ProviderTurn(
                self.name,
                self.model,
                "Okay—I won’t run that action.",
                interpreted_task="Respect an explicit negation attached to a local action request.",
            )
        actionable = request_text(text)
        if actionable is None:
            # Questions about how an action works must never execute it.
            return ProviderTurn(
                self.name,
                self.model,
                "I can explain that without making changes.",
                interpreted_task="The request needs general language reasoning beyond the limited offline router.",
            )
        text = actionable if not is_desktop_input_request(user_text) else text
        if requested := (
            website_request(actionable) or audio_request(actionable) or media_request(actionable)
        ):
            return ProviderTurn(
                self.name,
                self.model,
                "",
                tool_calls=[call(requested.tool, requested.arguments)],
                interpreted_task="Execute the exact everyday request through a dedicated local tool.",
            )
        lowered = text.casefold()
        interpreted = "Handle a local desktop request using the limited offline command router."
        plan: list[str] = []
        calls: list[ToolCall] = []
        direct = ""
        continuation: dict[str, Any] = {"original_text": text}
        subject = _recent_subject(context)
        recent_process = _recent_top_process(context)

        if is_negative_action(text):
            interpreted = "Respect an explicit negation attached to a local action request."
            plan = ["Do not select or execute an action tool"]
            direct = "Okay—I won’t run that action."
        elif is_desktop_input_request(user_text):
            interpreted = (
                "The desktop input request needs an exact supported target and explicit input."
            )
            plan = [
                "Keep requested text literal",
                "Require a resolvable window and explicit control, coordinates, or key",
            ]
            direct = 'Specify the window and input, for example: focus Firefox then type "hello", click "Save" in Firefox, or press Ctrl+L in Firefox.'
        elif re.fullmatch(
            r"(?:connect|enable|start)\s+(?:desktop|mouse\s+and\s+keyboard)\s+(?:input|control)[.!?]*",
            lowered,
        ):
            interpreted = "Connect session-scoped desktop input through KDE's native portal."
            calls = [call("desktop.input.connect")]
        elif re.fullmatch(
            r"(?:disconnect|disable)\s+(?:desktop|mouse\s+and\s+keyboard)\s+(?:input|control)[.!?]*",
            lowered,
        ):
            interpreted = "Disconnect the current desktop input session."
            calls = [call("desktop.input.disconnect")]
        elif re.fullmatch(r"(?:show|check)\s+(?:desktop\s+)?input\s+status[.!?]*", lowered):
            interpreted = "Inspect the current native desktop input session."
            calls = [call("desktop.input.status")]
        elif re.fullmatch(r"(?:how much|how much is it using)[?.!]*", lowered) and recent_process:
            interpreted = "Resolve a bounded follow-up to the most recently reported process."
            plan = ["Use the immediately preceding process result", "Report its measured usage"]
            direct = f"{recent_process[0]} was using {recent_process[1]} in the latest sample."
        elif re.fullmatch(r"(?:close|quit|exit)\s+(?:it|that)[.!?]*", lowered) and recent_process:
            query = application_query(recent_process[0])
            interpreted = f"Resolve the follow-up target {query} from recent context, then close that exact process."
            plan = [
                "Resolve the immediately preceding process entity",
                "Verify the live process",
                "Close it and verify termination",
            ]
            calls = [call("system.get_processes", {"query": query, "sort": "memory", "limit": 50})]
            continuation = {
                "original_text": f"close {query}",
                "spoken_text": text,
                "context_resolved": True,
            }
        elif re.search(r"\bhow\s+about\s+(?:ram|memory)\b", lowered):
            interpreted = "Interpret a contextual follow-up as current memory usage."
            plan = ["Query live memory counters", "Summarize used and available RAM"]
            calls = [call("system.get_memory_usage")]
        elif subject and re.fullmatch(
            r"(?:what(?:'s| is) using the most|(?:(?:what|which) )?(?:process|app|application) is using the most)"
            r"(?: right now)?[?.!]*",
            lowered,
        ):
            interpreted = f"Interpret a contextual follow-up as the largest {subject} consumers."
            plan = [
                f"Sample user-visible processes",
                f"Sort by {subject}",
                "Summarize the largest consumers",
            ]
            calls = [call("system.get_processes", {"limit": 8, "sort": subject})]
        elif recent_process and re.fullmatch(
            r"(?:what|which) (?:app|application) does (?:that|this|the) process (?:connect|belong) to[?.!]*",
            lowered,
        ):
            interpreted = "Look up the application associated with the last measured process."
            calls = [
                call(
                    "applications.list", {"query": application_query(recent_process[0]), "limit": 8}
                )
            ]
            continuation["lookup_only"] = True
        elif match := re.match(
            r"^\s*(?:e\.?\s*v\.?,?\s*)?remember\s+(?:that\s+)?(.+?)\s*[.!]?\s*$",
            text,
            re.IGNORECASE,
        ):
            content = match.group(1).strip()
            interpreted = "Store an explicit user-requested durable memory."
            plan = [
                "Confirm the exact memory content",
                "Store it in the local explicit-memory database",
            ]
            calls = [call("memory.remember", {"content": content})]
        elif re.search(
            r"\bwhat\s+do\s+you\s+remember\b|\bshow\s+(?:me\s+)?(?:your|my)\s+memories\b", lowered
        ):
            about = re.search(r"\babout\s+(.+?)[?.!]?\s*$", text, re.IGNORECASE)
            query = about.group(1).strip() if about else ""
            interpreted = "Search the user's explicit durable memories."
            plan = ["Query the local explicit-memory store", "Summarize matching records"]
            calls = [call("memory.search", {"query": query, "limit": 20})]
        elif re.search(
            r"\b(process|processes|what.?s using|what is using)\b", lowered
        ) and re.search(r"\b(cpu|ram|memory)\b", lowered):
            sort = "cpu" if "cpu" in lowered else "memory"
            interpreted = f"Identify processes using the most {sort}."
            plan = [
                f"Sample user-visible processes",
                f"Sort by {sort}",
                "Summarize the largest consumers",
            ]
            calls = [call("system.get_processes", {"limit": 8, "sort": sort})]
        elif re.search(r"\b(ram|memory)\b", lowered) and re.search(
            r"\b(using|usage|used|free|available|how much)\b", lowered
        ):
            interpreted = "Inspect current system memory usage."
            plan = ["Query live memory counters", "Summarize used and available RAM"]
            calls = [call("system.get_memory_usage")]
        elif re.search(
            r"\b(?:what(?:'s| is)|check|show me)\s+(?:my\s+|the\s+)?(?:ram|memory)\b", lowered
        ):
            interpreted = "Inspect current system memory usage."
            plan = ["Query live memory counters", "Summarize used and available RAM"]
            calls = [call("system.get_memory_usage")]
        elif re.search(r"\b(cpu|processor)\b", lowered) and re.search(
            r"\b(temp|temperature|hot|heat)\b", lowered
        ):
            interpreted = "Inspect the current CPU package temperature."
            plan = [
                "Read the preferred CPU package hwmon sensor",
                "Report the measured temperature",
            ]
            calls = [call("system.get_temperature")]
        elif re.search(r"\b(cpu|processor)\b", lowered) and re.search(
            r"\b(using|usage|load|busy)\b", lowered
        ):
            interpreted = "Inspect current CPU utilization."
            plan = ["Sample CPU utilization", "Report load and frequency"]
            calls = [call("system.get_cpu_usage")]
        elif re.search(r"\b(disk|storage|drive)\b", lowered) and re.search(
            r"\b(free|usage|space|full)\b", lowered
        ):
            interpreted = "Inspect root disk capacity."
            plan = ["Query root filesystem usage", "Report used and free space"]
            calls = [call("system.get_disk_usage", {"path": "/"})]
        elif re.search(r"\bfirewall\b", lowered):
            interpreted = (
                "Inspect the active firewall implementation without changing security state."
            )
            plan = [
                "Read OpenRC firewall service state",
                "Attempt an unprivileged ruleset inspection",
                "Report evidence and limitations",
            ]
            calls = [call("security.firewall")]
        elif re.search(
            r"\b(?:ports?|listening|exposed|network services?)\b", lowered
        ) and re.search(
            r"\b(?:network|ports?|services?|programs?|computer|listening|exposed)\b", lowered
        ):
            interpreted = "Inspect real local listening sockets and their binding scope."
            plan = [
                "Read TCP and UDP listeners",
                "Classify localhost versus network bindings",
                "Identify visible owning processes",
            ]
            calls = [call("security.network_exposure")]
        elif re.search(r"\bssh(?:d)?\b", lowered):
            interpreted = "Inspect the OpenRC SSH daemon without changing it."
            plan = ["Read sshd service state", "Inspect the readable configured port"]
            calls = [call("security.ssh")]
        elif re.search(r"\b(?:security updates?|glsa|advisories)\b", lowered):
            interpreted = "Run the explicit Gentoo GLSA advisory scan."
            plan = [
                "Run glsa-check on demand",
                "Report its limited advisory scope and measured result",
            ]
            calls = [call("security.updates")]
        elif re.search(r"\b(?:startup|autostart|persistence)\b", lowered):
            interpreted = "Inspect local startup and persistence entries."
            plan = ["Read user and system XDG autostart entries", "List user shell startup files"]
            calls = [call("security.startup")]
        elif re.search(r"\b(?:login|logged in|authentication activity)\b", lowered):
            interpreted = "Inspect recent login-session records."
            plan = ["Read recent wtmp sessions", "Report bounded evidence"]
            calls = [call("security.login_activity")]
        elif re.search(r"\be\.?v\.?\b", lowered) and re.search(
            r"\b(?:permissions?|security|secure|ipc)\b", lowered
        ):
            interpreted = "Audit E.V.'s own sensitive local paths and IPC permissions."
            plan = [
                "Inspect owners and mode bits",
                "Verify the IPC is a local Unix socket",
                "Report concrete findings",
            ]
            calls = [call("security.ev")]
        elif re.search(r"\b(?:accessibility|semantic\s+(?:ui|controls?|elements?))\b", lowered):
            interpreted = "Inspect the real semantic desktop-control backend."
            plan = [
                "Read the current AT-SPI session state",
                "Report which applications expose usable semantic controls",
            ]
            calls = [call("accessibility.status")]
        elif re.search(
            r"\b(?:read|recognize|scan|ocr|see|look\s+at)\b.*\b(?:screen|desktop|window|display|screenshot|text)\b",
            lowered,
        ):
            interpreted = "Read visible screen text through a private local capture and OCR."
            plan = [
                "Capture locally",
                "Run isolated local OCR",
                "Return recognized text without pointer input",
            ]
            calls = [call("vision.capture")]
            continuation["ocr_after_capture"] = True
        elif re.search(
            r"\b(?:take|grab|make|capture)\b.*\b(?:screen\s*shot|screen|display)\b", lowered
        ):
            interpreted = "Capture one private, expiring local screenshot."
            plan = ["Capture locally", "Verify the PNG", "Expire it automatically"]
            calls = [call("vision.capture")]
        elif re.search(r"\b(?:vision|screen perception|visual fallback)\b", lowered):
            interpreted = "Inspect the real local visual-perception backend."
            plan = [
                "Inspect capture availability",
                "Report understanding, pointer, upload, and retention boundaries",
            ]
            calls = [call("vision.status")]
        elif re.search(r"\b(?:coding agent|codex|fix yourself|self.?improv)\b", lowered):
            interpreted = "Inspect the controlled coding-agent gateway without editing anything."
            plan = [
                "Discover the real agent executable",
                "Report approval and checkpoint policy",
                "Do not fake unavailable integration",
            ]
            calls = [call("development.coding_agent_status")]
        elif re.search(r"\b(?:security|secure|suspicious)\b", lowered):
            interpreted = "Run a local read-only device security overview."
            plan = [
                "Inspect firewall",
                "Inspect network exposure and SSH",
                "Inspect startup and login activity",
                "Audit E.V. permissions",
                "Derive status from findings",
            ]
            calls = [call("security.overview")]
        elif re.search(r"\b(?:microphone|mic)\b", lowered) and re.search(
            r"\b(?:what|which|current|using|selected)\b", lowered
        ):
            interpreted = "Identify the exact current PipeWire microphone."
            plan = [
                "List real audio endpoints",
                "Match the default input name",
                "Report its device description",
            ]
            calls = [call("audio.devices")]
        elif re.search(
            r"\b(?:use|switch|change|set)\b.*\b(?:built.?in|laptop|pc|computer)\b.*\b(?:microphone|mic)\b",
            lowered,
        ):
            interpreted = "Resolve and select the built-in microphone without guessing its PipeWire identifier."
            plan = [
                "List real audio inputs",
                "Require one built-in input",
                "Set and verify it as default",
            ]
            calls = [call("audio.devices")]
            continuation["audio_target"] = "built_in_input"
        elif re.search(
            r"\b(?:use|switch|change|set)\b.*\b(?:airpods?|bluetooth)\b.*\b(?:microphone|mic)\b",
            lowered,
        ):
            interpreted = "Resolve and select the connected Bluetooth microphone without guessing its PipeWire identifier."
            plan = [
                "List real audio inputs",
                "Require one Bluetooth input",
                "Set and verify it as default",
            ]
            calls = [call("audio.devices")]
            continuation["audio_target"] = "bluetooth_input"
        elif re.search(r"\b(?:unmute|turn on|enable)\b.*\b(?:microphone|mic)\b", lowered):
            interpreted = "Unmute the exact current default microphone."
            plan = ["Change the default input mute state", "Verify the observed state"]
            calls = [call("audio.microphone_mute.set", {"muted": False})]
        elif re.search(r"\b(?:mute|turn off|disable)\b.*\b(?:microphone|mic)\b", lowered):
            interpreted = "Mute the exact current default microphone."
            plan = ["Change the default input mute state", "Verify the observed state"]
            calls = [call("audio.microphone_mute.set", {"muted": True})]
        elif re.search(
            r"\b(?:audio|sound)\s+devices?\b|\b(?:speakers?|microphones?|headphones?)\b.*\b(?:list|show|available|connected)\b",
            lowered,
        ):
            interpreted = "List real PipeWire audio inputs, outputs, and defaults."
            plan = [
                "Query PipeWire through pactl",
                "Report bounded device descriptions and defaults",
            ]
            calls = [call("audio.devices")]
        elif re.search(r"\b(volume|sound)\b", lowered) and re.search(
            r"\b(what|current|level|how loud)\b", lowered
        ):
            interpreted = "Read current output volume."
            plan = ["Query the default PipeWire output", "Report volume and mute state"]
            calls = [call("audio.get_volume")]
        elif re.match(
            r"^(?:set|turn|put|change|increase|decrease|lower|raise|mute|unmute)\b", lowered
        ) and re.search(r"\b(?:volume|sound|mute|unmute)\b", lowered):
            direct = "I didn't change any audio. For the system output, say 'set volume to 40 percent', 'lower volume by 5', or 'mute'. App-specific volume needs an exact supported app control."
            interpreted = "Do not redirect an unsupported volume request to a different target."
        elif match := _CLOSE_APPLICATION.match(text):
            query = application_query(match.group(1))
            interpreted = f"Resolve the running application named {query} without guessing, then close that exact process."
            plan = [
                "Find exact running-process matches",
                "Resolve one top-level application process",
                "Close it and verify termination",
            ]
            calls = [call("system.get_processes", {"query": query, "sort": "memory", "limit": 50})]
        elif re.search(r"\b(?:show|open)\b.*\b(?:e\.?\s*v\.?|control center)\b", lowered):
            interpreted = "Open or focus the E.V. Control Center."
            plan = ["Activate the trusted E.V. desktop entry"]
            calls = [call("applications.focus", {"desktop_id": "ev-control-center"})]
        elif match := re.fullmatch(
            r"\s*(?:please\s+)?(?:and\s+)?find\s+(?:me\s+)?(?:file|the\s+file|a\s+file)\s+['\"]?([^'\"]+?)['\"]?(?:\s+in\s+.+)?[.!?]*\s*",
            text,
            re.IGNORECASE,
        ):
            query = match.group(1).strip().rstrip("?.")
            interpreted = f"Find files named like {query}."
            plan = [
                "Search the configured Downloads root by filename",
                "Return bounded matching paths",
            ]
            calls = [
                call(
                    "files.find",
                    {"query": query, "root": str(Path.home() / "Downloads"), "limit": 20},
                )
            ]
        elif match := re.fullmatch(
            r"\s*(?:please\s+)?(?:and\s+)?find\s+(?:me\s+)?(?:the\s+)?(?:app(?:lication)?\s+)?(.+?)[.!?]*\s*",
            text,
            re.IGNORECASE,
        ):
            query = application_query(match.group(1))
            interpreted = f"Find the installed or currently running application matching {query}."
            plan = ["Search the unified application catalog", "Include live windows and processes"]
            calls = [call("applications.list", {"query": query, "limit": 8})]
            continuation["lookup_only"] = True
        elif match := re.fullmatch(
            r"\s*why\s+(?:can(?:not|'t)|could(?:not|'t))\s+you\s+find\s+(?:the\s+)?(.+?)[.!?]*\s*",
            text,
            re.IGNORECASE,
        ):
            query = application_query(match.group(1))
            interpreted = f"Check the unified application catalog for {query} instead of guessing why it was missed."
            plan = ["Search installed applications", "Cross-check open windows and user processes"]
            calls = [call("applications.list", {"query": query, "limit": 8})]
            continuation["lookup_only"] = True
        elif (query := _open_application_name(text)) is not None:
            interpreted = f"Find the installed application matching {query}."
            plan = ["Search trusted desktop entries", "Identify the best application match"]
            calls = [call("applications.list", {"query": query, "limit": 8, "launch_only": True})]
        elif re.search(r"\b(battery|charge)\b", lowered):
            interpreted = "Read the current battery state."
            plan = ["Query the system battery", "Report charge and power state"]
            calls = [call("system.get_battery")]
        elif re.search(r"\b(network|internet|wifi|wi-fi)\b", lowered):
            interpreted = "Inspect local network-interface state."
            plan = [
                "Read interface link state",
                "Summarize connected interfaces and traffic counters",
            ]
            calls = [call("system.get_network_status")]
        else:
            direct = (
                "I’m online locally, but the general language model is not configured yet. "
                "I can already inspect CPU, temperature, RAM, disk, processes, battery, network, volume, applications, and files."
            )
            interpreted = (
                "The request needs general language reasoning beyond the limited offline router."
            )
            plan = ["Report the missing remote reasoning provider honestly"]

        return ProviderTurn(
            provider=self.name,
            model=self.model,
            text=direct,
            tool_calls=calls,
            interpreted_task=interpreted,
            plan=plan,
            latency_ms=(time.perf_counter() - started) * 1000,
            continuation=continuation,
        )

    async def continue_with_tools(
        self,
        turn: ProviderTurn,
        outputs: list[tuple[ToolCall, dict[str, Any]]],
        tools: list[dict[str, Any]],
    ) -> ProviderTurn:
        started = time.perf_counter()
        if not outputs:
            return turn
        tool_call, wrapper = outputs[-1]
        result = wrapper.get("result", wrapper)
        name = tool_call.name
        text: str
        if wrapper.get("status") == "denied":
            text = "Okay—I did not run that action."
        elif name == "system.clock":
            if not result.get("local_time") or not result.get("local_date"):
                text = "I couldn't read the system clock."
            elif (turn.continuation or {}).get("clock_display") == "date":
                text = f"Today is {result['local_date']}."
            else:
                text = f"It's {result['local_time']} {result.get('timezone', '')}.".replace(
                    " .", "."
                )
            if (turn.continuation or {}).get("timer_missing"):
                text = "There isn't an active timer. " + text
        elif name == "reminders.list":
            reminders = result.get("reminders")
            if reminders == []:
                return ProviderTurn(
                    self.name,
                    self.model,
                    "",
                    tool_calls=[call("system.clock")],
                    continuation={"clock_display": "time", "timer_missing": True},
                    interpreted_task="No timer exists; read the clock in case 'timer' was a misheard time request.",
                )
            text = (
                "Pending timers and reminders: "
                + ", ".join(str(item.get("label", "Unnamed reminder")) for item in reminders[:5])
                if isinstance(reminders, list)
                else "I couldn't read the pending timers."
            )
        elif name == "system.get_memory_usage":
            memory = result["memory"]
            gib = 1024**3
            text = f"RAM is {memory['percent']:.1f}% used. About {memory['used_bytes']/gib:.1f} GB is used and {memory['available_bytes']/gib:.1f} GB is available."
        elif name == "system.get_temperature":
            temperature = result.get("celsius")
            text = (
                "I couldn’t find a readable CPU temperature sensor."
                if temperature is None
                else f"The CPU package is currently {temperature:.0f} degrees Celsius."
            )
        elif name == "system.get_cpu_usage":
            text = f"CPU usage is currently {result['percent']:.1f}%, with a one-minute load average of {result['load_average'][0]:.2f}."
        elif name == "system.get_processes":
            rows = result.get("processes", [])[:5]
            original = str((turn.continuation or {}).get("original_text", ""))
            close_match = _CLOSE_APPLICATION.match(original)
            if close_match:
                all_rows = result.get("processes", [])
                matched_pids = {row["pid"] for row in all_rows}
                roots = [row for row in all_rows if row.get("ppid") not in matched_pids]
                if len(roots) > 1:
                    parents_with_children = {row.get("ppid") for row in all_rows}
                    application_roots = [
                        row for row in roots if row.get("pid") in parents_with_children
                    ]
                    if len(application_roots) == 1:
                        roots = application_roots
                if len(roots) == 1:
                    target = roots[0]
                    return ProviderTurn(
                        self.name,
                        self.model,
                        "",
                        [
                            call(
                                "applications.close_process",
                                {
                                    "pid": target["pid"],
                                    "started_at_epoch": target["started_at_epoch"],
                                    "expected_query": application_query(close_match.group(1)),
                                },
                            )
                        ],
                        turn.interpreted_task,
                        turn.plan,
                        continuation=turn.continuation,
                    )
                if not roots:
                    text = f"I couldn’t find a running application matching {application_query(close_match.group(1))}."
                else:
                    names = ", ".join(f"{row['name']} (PID {row['pid']})" for row in roots[:5])
                    text = f"I found multiple possible top-level matches: {names}. Please be more specific."
            elif not rows:
                text = "I couldn’t read any matching processes."
            elif result.get("sort") == "cpu":
                text = (
                    "Top CPU users are "
                    + ", ".join(f"{row['name']} at {row['cpu_percent']:.1f}%" for row in rows)
                    + "."
                )
            else:
                text = (
                    "Top memory users are "
                    + ", ".join(
                        f"{row['name']} at {row['rss_bytes']/1024**2:.0f} MB" for row in rows
                    )
                    + "."
                )
        elif name == "system.get_disk_usage":
            gib = 1024**3
            text = f"The root disk is {result['percent']:.1f}% used, with {result['free_bytes']/gib:.1f} GB free."
        elif name == "audio.get_volume":
            text = f"Volume is {result.get('percent')}% and is {'muted' if result.get('muted') else 'not muted'}."
        elif name in {"audio.set_volume", "audio.adjust_volume"}:
            text = (
                str(result.get("message"))
                if result.get("set") and result.get("message")
                else "I couldn’t verify the requested volume."
            )
        elif name == "audio.set_mute":
            text = (
                str(result.get("message"))
                if result.get("set") and result.get("message")
                else "I couldn’t verify the requested mute state."
            )
        elif name == "browser.open_url":
            text = str(result.get("message", "The browser URL request did not return a result."))
        elif name == "audio.media":
            text = str(
                result.get("message") or result.get("reason") or "I couldn’t verify playback."
            )
        elif name == "system.get_battery":
            text = (
                "No battery was detected."
                if not result.get("present")
                else f"Battery is at {result['percent']:.0f}% and is {'plugged in' if result['plugged'] else 'on battery power'}."
            )
        elif name == "system.get_network_status":
            up = [
                item["name"]
                for item in result.get("interfaces", [])
                if item.get("up") and item["name"] != "lo"
            ]
            text = "Connected interfaces: " + (", ".join(up) if up else "none detected") + "."
        elif name == "security.firewall":
            if result.get("status") == "ACTIVE":
                implementations = (
                    ", ".join(result.get("active_implementations", [])) or "a firewall service"
                )
                text = f"Your firewall is active through {implementations}."
            else:
                text = "I couldn't confirm an active firewall service. The Security view has the evidence and limitations."
        elif name == "security.network_exposure":
            total = int(result.get("listener_count", 0))
            exposed = int(result.get("network_accessible_count", 0))
            local = int(result.get("localhost_only_count", 0))
            text = f"I found {total} listening sockets: {local} localhost-only and {exposed} bound beyond localhost."
        elif name == "security.ssh":
            text = f"SSH is {'running' if result.get('running') else 'not running'}."
        elif name == "security.startup":
            text = f"I found {result.get('enabled_count', 0)} enabled desktop startup entries."
        elif name == "security.login_activity":
            text = (
                f"I found {result.get('count', 0)} recent login records. Details are in Security."
            )
        elif name == "security.ev":
            count = len(result.get("findings", []))
            text = (
                "E.V.'s sensitive local files and IPC permissions look private."
                if count == 0
                else f"E.V.'s permission audit found {count} item{'s' if count != 1 else ''} to review."
            )
        elif name == "security.updates":
            text = (
                "No unresolved Gentoo GLSA advisories were reported."
                if result.get("status") == "NO_UNRESOLVED_GLSA_REPORTED"
                else f"The Gentoo advisory scan status is {result.get('status', 'unknown')}."
            )
        elif name == "security.overview":
            counts = result.get("derived_from", {})
            text = (
                f"Security check complete. I found {counts.get('high', 0)} high-priority, "
                f"{counts.get('medium', 0)} review, and {counts.get('info', 0)} informational items."
            )
        elif name == "development.coding_agent_status":
            text = (
                "The controlled coding agent is available and remains approval-gated."
                if result.get("available")
                else f"The coding-agent gateway isn't available yet: {result.get('reason', 'Codex is unavailable')}."
            )
        elif name == "accessibility.status":
            if result.get("status") == "READY":
                text = f"Semantic controls are ready in {result.get('controllable_application_count', 0)} applications."
            else:
                text = f"Accessibility is limited right now. {result.get('reason', 'No applications are exposing usable controls')}."
        elif name == "vision.status":
            if result.get("available"):
                text = "Private local screenshots are ready. Screen text can be read with local OCR; desktop input has a separate session status."
            else:
                text = f"Screen capture isn't available: {result.get('reason', 'the capture backend is missing')}."
        elif name == "vision.capture":
            if result.get("captured") and (turn.continuation or {}).get("ocr_after_capture"):
                return ProviderTurn(
                    self.name,
                    self.model,
                    "",
                    [call("vision.ocr", {"capture_id": result["capture_id"]})],
                    turn.interpreted_task,
                    turn.plan,
                    continuation=turn.continuation,
                )
            text = (
                "Captured it locally. The private image expires automatically in ten minutes."
                if result.get("captured")
                else "I couldn't capture the screen."
            )
        elif name == "vision.ocr":
            recognized = str(result.get("text", "")).strip()
            text = (
                f"Visible text: {recognized[:1500]}"
                if recognized
                else "I captured the screen, but local OCR did not find readable text."
            )
        elif name.startswith("desktop.input."):
            if name.endswith("disconnect"):
                text = (
                    "Desktop input is disconnected."
                    if result.get("verified")
                    else "I could not verify that desktop input disconnected."
                )
            else:
                text = str(
                    result.get("reason")
                    or (
                        "Desktop input is connected."
                        if result.get("verified")
                        else "Desktop input is not connected."
                    )
                )
        elif name == "files.find":
            matches = result.get("results", [])
            text = (
                "I found "
                + (
                    ", ".join(item["path"] for item in matches[:5])
                    if matches
                    else "no matching files"
                )
                + "."
            )
        elif name == "applications.list":
            applications = result.get("applications", [])
            top_score = int(applications[0].get("match_score", 0)) if applications else 0
            top_matches = [
                app for app in applications if int(app.get("match_score", 0)) == top_score
            ]
            lookup_only = bool((turn.continuation or {}).get("lookup_only"))
            observed = result.get("observed_running", [])
            if lookup_only and applications:
                descriptions = [
                    f"{application['name']} ({'running' if application.get('running') else 'installed'})"
                    for application in applications[:6]
                ]
                text = "Matching applications: " + ", ".join(descriptions) + "."
            elif lookup_only and observed:
                text = (
                    "Running applications without an installed launcher: "
                    + ", ".join(str(item["name"]) for item in observed[:6])
                    + "."
                )
            elif lookup_only:
                text = "I couldn’t find that in installed applications, open windows, or running app processes."
            elif len(applications) == 1 or (top_score > 0 and len(top_matches) == 1):
                application = applications[0]
                return ProviderTurn(
                    self.name,
                    self.model,
                    "",
                    [call("applications.open", {"desktop_id": application["desktop_id"]})],
                    turn.interpreted_task,
                    turn.plan,
                    continuation=turn.continuation,
                )
            elif applications:
                text = (
                    "Matching applications: "
                    + ", ".join(app["name"] for app in applications[:6])
                    + "."
                )
            else:
                text = "I couldn’t find a matching installed application."
        elif name == "applications.open":
            if result.get("launched"):
                text = f"Opening {result.get('name') or 'the application'}."
            elif result.get("activation_status") == "unverified":
                text = "The launcher hasn’t confirmed yet. The app may have opened; I haven’t launched it again."
            else:
                text = "I couldn’t open that application."
        elif name == "applications.focus":
            text = (
                "I opened the E.V. Control Center."
                if result.get("launched")
                else "I couldn’t open the E.V. Control Center."
            )
        elif name == "applications.close_process":
            target = str(result.get("expected_query") or result.get("name") or "the application")
            text = (
                f"I closed {target}."
                if result.get("closed")
                else f"{target} is still running, so I did not claim it closed."
            )
        elif name == "audio.devices":
            desired = str((turn.continuation or {}).get("audio_target", ""))
            inputs = result.get("inputs", [])
            if desired:
                bus = "pci" if desired == "built_in_input" else "bluetooth"
                matches = [item for item in inputs if item.get("device_bus") == bus]
                if len(matches) == 1:
                    return ProviderTurn(
                        self.name,
                        self.model,
                        "",
                        [call("audio.default_input.set", {"source": matches[0]["name"]})],
                        turn.interpreted_task,
                        turn.plan,
                        continuation=turn.continuation,
                    )
                label = "built-in" if bus == "pci" else "Bluetooth"
                text = f"I found {len(matches)} {label} microphone choices, so I didn't guess."
            else:
                default_name = str(result.get("default_input", ""))
                default = next((item for item in inputs if item.get("name") == default_name), None)
                if default:
                    description = (
                        default.get("description")
                        or default.get("device_description")
                        or default_name
                    )
                    text = f"The current microphone is {description}."
                else:
                    text = "PipeWire did not identify a current default microphone."
        elif name == "audio.default_input.set":
            text = (
                "I switched the default microphone."
                if result.get("verified")
                else "The microphone change did not verify, so I did not claim it switched."
            )
        elif name == "audio.default_output.set":
            text = (
                "I switched the default audio output."
                if result.get("verified")
                else "The output change did not verify, so I did not claim it switched."
            )
        elif name == "audio.microphone_mute.set":
            if result.get("verified"):
                text = (
                    "The microphone is muted."
                    if result.get("muted")
                    else "The microphone is unmuted."
                )
            else:
                text = "The microphone mute change did not verify."
        elif name == "memory.search":
            memories = result.get("memories", [])
            text = (
                "I don’t have any matching explicit memories."
                if not memories
                else "I remember: " + "; ".join(item["content"] for item in memories[:6])
            )
        elif name == "memory.remember":
            text = (
                "I stored that as an explicit local memory."
                if result.get("memory")
                else "I couldn’t store that memory."
            )
        elif name == "memory.forget":
            text = (
                "I removed that explicit memory."
                if result.get("removed")
                else "That memory no longer exists."
            )
        else:
            text = f"{name} completed successfully."
        return ProviderTurn(
            provider=self.name,
            model=self.model,
            text=text,
            interpreted_task=turn.interpreted_task,
            plan=turn.plan,
            usage={},
            latency_ms=(time.perf_counter() - started) * 1000,
        )
