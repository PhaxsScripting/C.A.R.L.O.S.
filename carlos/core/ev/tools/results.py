"""Evidence classification shared by direct, deterministic and model actions.

An accepted dispatch is not a verified effect. Unknown tools fail conservatively
to EXECUTED_UNVERIFIED rather than inheriting a success claim from their prose.
"""

from dataclasses import asdict, dataclass
from typing import Any

# Audited observation primitives, not utterance matching. New tools opt in via
# ToolSpec.read_only; SAFE permission alone does not imply read-only (launch is SAFE).
OBSERVATION_TOOLS = frozenset("""
accessibility.status accessibility.elements.list
applications.list applications.alias.list audio.devices audio.get_volume
desktop.world desktop.windows.list desktop.window_information desktop.output.resolve
desktop.workspace.resolve desktop.window.resolve desktop.window.wait desktop.input.status
desktop.clipboard_read desktop.controls.list
development.find_project development.git_status development.inspect_build_error
development.coding_agent_status development.coding_agent_result
files.find files.info files.list files.read files.hash files.recent
system.clock system.identity system.get_cpu_usage system.get_memory_usage
system.get_disk_usage system.get_network_status system.get_battery system.get_temperature
system.get_processes system.devices system.mounts system.openrc_services
system.boot.status system.startup.list system.power.status
settings.overview settings.brightness.get settings.radios.status settings.audio.devices
settings.audio.apps security.overview security.firewall security.firewall.runtime
security.network_exposure security.ssh security.startup security.login_activity
security.updates security.ev spotify.status spotify.diagnose spotify.search spotify.playlists
memory.search reminders.list routines.list scenes.list notes.list notes.read
tasks.list tasks.read bookmarks.list bookmarks.read snippets.list snippets.read
utility.calculate utility.convert utility.world_clock utility.clipboard_stats
interaction.selection_status vision.status vision.ocr web.search web.fetch
""".split())


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    ok: bool
    status: str
    verified: bool
    error: str | None = None
    retryable: bool = False
    verification_hint: str = ""
    changed_state: bool | None = None
    scope: str = "tool_effect"

    def public(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_result(name: str, data: dict[str, Any], *, read_only: bool = False) -> ExecutionResult:
    read_only = read_only or name in OBSERVATION_TOOLS
    changed = False if read_only else None
    error = data.get("error")
    if data.get("ok") is False or error or data.get("truncated"):
        return ExecutionResult(
            False,
            "FAILED",
            False,
            str(error or "Tool failed or output was truncated"),
            False,
            "Inspect the failure before repeating any action",
            changed,
        )

    # Native launch/dispatch acknowledgements cannot verify application state.
    accepted_fields = {
        "applications.open": "launched",
        "applications.focus": "launched",
        "browser.open_url": "requested",
        "bookmarks.open": "requested",
        "settings.open": "requested",
        "files.open_selected": "requested",
        "development.source.open": "requested",
        "accessibility.element.activate": "action_accepted",
        "desktop.controls.activate": "action_accepted",
    }
    delivery = (
        name.startswith(("desktop.keyboard.", "desktop.pointer."))
        and name not in {"desktop.pointer.move", "desktop.pointer.move_relative"}
    ) or name in {"browser.shortcut", "desktop.input.gesture"}
    if name in accepted_fields or delivery:
        field = "input_sent" if delivery else accepted_fields[name]
        accepted = data.get(field, data.get("verified", False)) is True
        return ExecutionResult(
            accepted,
            "EXECUTED_UNVERIFIED" if accepted else "FAILED",
            False,
            None if accepted else "Backend did not acknowledge the request",
            False,
            "Observe the target application's resulting state",
            None,
        )
    if name == "system.power":
        # A scheduled request is not evidence that the machine powered off.
        accepted = data.get("scheduled") is True and data.get("verified") is True
        return ExecutionResult(
            accepted,
            "EXECUTED_UNVERIFIED" if accepted else "FAILED",
            False,
            None if accepted else "Power request was not scheduled",
            verification_hint="Power request scheduled; final OS transition is not verified",
        )
    if name == "vision.capture":
        verified = data.get("captured") is True
        return ExecutionResult(
            verified,
            "SUCCEEDED_VERIFIED" if verified else "FAILED",
            verified,
            None if verified else "Capture not produced",
            scope="capture_only",
        )
    if name in {"vision.describe", "vision.inspect_window"}:
        accepted = isinstance(data.get("description"), str) and bool(data["description"].strip())
        return ExecutionResult(
            accepted,
            "EXECUTED_UNVERIFIED" if accepted else "FAILED",
            False,
            None if accepted else "No visual inference returned",
            changed_state=False,
            verification_hint="Model interpretation is not independently observed scene truth",
            scope="visual_inference",
        )
    if name == "development.project.run":
        verified = data.get("verified") is True and data.get("exit_code") == 0
        return ExecutionResult(
            verified,
            "SUCCEEDED_VERIFIED" if verified else "FAILED",
            verified,
            None if verified else "Project command did not exit successfully",
            scope="process_exit_only",
        )
    if "verified" in data or name.endswith(".resolve") or name == "desktop.window.wait":
        verified = data.get("verified", data.get("resolved")) is True
        return ExecutionResult(
            verified,
            "SUCCEEDED_VERIFIED" if verified else "FAILED",
            verified,
            None if verified else "Postcondition was not met",
            False,
            "Backend postcondition readback",
            changed,
        )
    if read_only and data:
        return ExecutionResult(
            True,
            "SUCCEEDED_VERIFIED",
            True,
            verification_hint="Observation returned",
            changed_state=False,
            scope="observation_only",
        )
    return ExecutionResult(
        True,
        "EXECUTED_UNVERIFIED",
        False,
        verification_hint="No postcondition verifier is implemented for this result",
        changed_state=changed,
    )
