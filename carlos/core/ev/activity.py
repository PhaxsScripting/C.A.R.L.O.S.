"""HUD projection of execution events, never model claims or invented progress."""


class AgentActivity:
    def __init__(self):
        self.data = {
            "phase": "IDLE",
            "task_id": "",
            "action": "",
            "steps_completed": 0,
            "steps_total": 0,
            "goal_verified": False,
            "wait_reason": "",
            "engineering": {},
        }
        self._waiting = False
        self._task_running = False

    def snapshot(self):
        return dict(self.data)

    def consume(self, event):
        before = dict(self.data)
        kind, p = event.type, event.payload
        if kind.startswith("coding."):
            import time

            previous = self.data.get("engineering", {})
            identifier = p.get("proposal_id", event.correlation_id)
            if kind == "coding.started":
                self.data["engineering"] = {
                    "proposal_id": identifier,
                    "state": "RUNNING",
                    "project": p.get("project", ""),
                    "started_epoch": p.get("started_epoch"),
                    "files_changed": 0,
                }
            elif identifier == previous.get("proposal_id"):
                states = {
                    "coding.cancelled": "CANCELLED",
                    "coding.failed": "FAILED",
                    "coding.completed": p.get("status", "FINISHED"),
                }
                self.data["engineering"] = {
                    **previous,
                    "state": states.get(kind, previous.get("state")),
                    "message": p.get("message", p.get("failure", previous.get("message", ""))),
                    "files_changed": p.get("files_changed", previous.get("files_changed", 0)),
                    "observed_monotonic": time.monotonic(),
                }
            return self.data != before
        if kind == "task.started":
            self._waiting = False
            self._task_running = True
            self.data.update(
                phase="UNDERSTANDING",
                task_id=p.get("task_id", event.correlation_id),
                action="",
                steps_completed=0,
                steps_total=0,
                goal_verified=False,
                wait_reason="",
            )
        elif kind == "task.wait_state":
            self._waiting = p.get("phase") != "WAIT_FINISHED"
            self.data.update(
                phase="WAITING" if self._waiting else "EXECUTING",
                wait_reason="Observing declared conditions" if self._waiting else "",
            )
        elif kind == "task.updated":
            self._waiting = False
            self._task_running = False
            status = p.get("status")
            verified = status == "completed" and p.get("goal_verified") is True
            phase = {
                "cancelled": "CANCELLED",
                "failed": "FAILED",
                "offline": "BLOCKED",
                "confirmation_required": "WAITING_FOR_USER",
                "blocked": "BLOCKED",
            }.get(
                status,
                (
                    "COMPLETED"
                    if verified
                    else (
                        "ANSWERED"
                        if p.get("execution_status") == "ANSWERED"
                        else "FINISHED_UNVERIFIED"
                    )
                ),
            )
            self.data.update(phase=phase, goal_verified=verified, wait_reason="", action="")
        elif kind == "core.state_changed":
            state = p.get("to")
            phase = {
                "LISTENING": "LISTENING",
                "TRANSCRIBING": "TRANSCRIBING",
                "THINKING": "UNDERSTANDING",
                "RETRIEVING_MEMORY": "UNDERSTANDING",
                "SPEAKING": "SPEAKING",
                "OFFLINE": "BLOCKED",
                "WAITING_FOR_CONFIRMATION": "WAITING_FOR_USER",
                "ERROR": "FAILED",
            }.get(state)
            if phase and not self._waiting:
                self.data["phase"] = phase
            elif state == "DORMANT" and self.data["phase"] in {
                "LISTENING",
                "SPEAKING",
                "TRANSCRIBING",
            }:
                self.data["phase"] = "IDLE"
        elif not self._waiting:
            if kind == "plan.created":
                self.data.update(
                    phase="PLANNING", steps_completed=0, steps_total=len(p.get("steps", []))
                )
            elif kind == "plan.step_completed":
                self.data["steps_completed"] = min(
                    self.data["steps_total"], self.data["steps_completed"] + 1
                )
            elif kind == "tool.started":
                tool = str(p.get("tool", ""))
                observing = p.get("read_only") is True or tool.startswith(
                    ("vision.", "desktop.observe", "desktop.controls.inspect")
                )
                self.data.update(
                    action=tool,
                    phase=(
                        "VERIFYING"
                        if tool == "agent.verify_conditions"
                        or (observing and self.data["phase"] == "VERIFYING")
                        else "OBSERVING" if observing else "EXECUTING"
                    ),
                )
            elif kind == "plan.goal_verifying":
                self.data["phase"] = "VERIFYING"
            elif kind in {"plan.recovering", "plan.step_retrying"}:
                self.data["phase"] = "RECOVERING"
            elif kind in {"command.failed", "plan.failed"}:
                self.data["phase"] = "CANCELLED" if p.get("cancelled") else "FAILED"
                if kind == "command.failed":
                    self._task_running = False
            elif kind in {"tool.completed", "tool.failed"} and not self._task_running:
                self.data.update(phase="FAILED" if kind == "tool.failed" else "IDLE", action="")
            elif kind in {"plan.cancel_requested", "plan.cancelled"}:
                self.data["phase"] = "CANCELLING" if kind.endswith("requested") else "CANCELLED"
        return self.data != before
