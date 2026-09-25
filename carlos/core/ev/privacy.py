"""Assistant privacy policy. This is not a sandbox for the logged-in OS user."""

from __future__ import annotations

import json
import os
import tempfile

MODES = frozenset({"NORMAL", "LOCAL ONLY", "PRIVATE SESSION", "DO NOT LISTEN", "GUEST"})
GUEST_TOOLS = frozenset({"system.get_cpu_usage", "system.get_memory_usage", "system.get_time"})


class PrivacyPolicy:
    def __init__(self, core):
        self.core = core
        self.changing = False

    @property
    def mode(self):
        return self.core.config.get("carlos", {}).get("privacy_mode", "NORMAL")

    @property
    def ephemeral(self):
        return self.mode in {"PRIVATE SESSION", "GUEST"}

    def apply_storage(self):
        c = self.core
        c.memory.set_private(self.ephemeral, guest=self.mode == "GUEST")
        c.task_journal.set_private(self.ephemeral)
        c.daily.set_private(self.ephemeral, guest=self.mode == "GUEST")
        c.bus.private = self.ephemeral
        c.logger.disabled = self.ephemeral
        c.bus._history.clear()
        if hasattr(c, "failure_reference"):
            c.failure_reference.clear()

    def tool_error(self, name):
        if self.changing:
            return "Privacy policy is changing; retry after it finishes"
        if self.mode == "GUEST" and name not in GUEST_TOOLS:
            return "Guest mode restricts personal context and desktop tools"
        if self.mode in {"LOCAL ONLY", "PRIVATE SESSION"} and name.startswith("plugin."):
            try:
                if self.core.tools.get(name).offline_available is not True:
                    return "Network-capable integrations are disabled in this privacy mode"
            except ValueError:
                return "Unknown integration tool"
        if self.mode in {"LOCAL ONLY", "PRIVATE SESSION", "GUEST"} and (
            name.startswith(("web.", "coding_agent.", "development.coding_agent", "spotify."))
            or name in {"vision.analyze", "development.project.run"}
        ):
            return "This tool can contact an external service and is disabled in this privacy mode"
        return ""

    def guard_provider(self):
        if self.mode in {
            "LOCAL ONLY",
            "PRIVATE SESSION",
            "GUEST",
        } and self.core.brain.provider.name not in {
            "offline",
            "local_hybrid",
            "local_agent",
            "carlos_router",
        }:
            from .ai.base import ProviderError

            raise ProviderError(
                "Cloud provider disabled by privacy policy; select Carlos local routing"
            )

    async def set_mode(self, mode):
        mode = str(mode).upper().replace("_", " ")
        if mode not in MODES:
            raise ValueError("Unknown privacy mode")
        if mode == self.mode:
            return {"mode": mode, "changed": False}
        if self.changing:
            raise ValueError("A privacy transition is already in progress")
        c = self.core
        self.changing = True
        try:
            # Stop generation and revoke outstanding approvals before changing
            # context or provider policy. Never migrate a pending approval.
            await c._stop_all_actions("privacy-transition")
            c.config.setdefault("carlos", {})["privacy_mode"] = mode
            self.apply_storage()
            c.planner.last_entities.clear()
            await c.voice.set_privacy_mode(mode == "DO NOT LISTEN")
            # Atomic mode-only update preserves other current user settings.
            data = json.loads(c.paths.config_file.read_text())
            data.setdefault("carlos", {})["privacy_mode"] = mode
            fd, temporary = tempfile.mkstemp(prefix=".carlos-mode-", dir=c.paths.config_dir)
            try:
                with os.fdopen(fd, "w") as output:
                    json.dump(data, output, indent=2)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, c.paths.config_file)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            c.bus.publish("carlos.privacy_changed", "privacy", {"mode": mode})
            return {
                "mode": mode,
                "changed": True,
                "conversational_memory": "RAM_ONLY" if self.ephemeral else "PERSISTENT",
            }
        finally:
            self.changing = False
