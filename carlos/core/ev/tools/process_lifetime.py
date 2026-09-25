"""Read-only Linux process lifetime observations; never signals a process."""

import os
from pathlib import Path

from .base import ToolSpec
from .builtin import object_schema
from ..permissions import Permission


def process_lifetime(arguments, context):
    pid = arguments["pid"]
    from ev.platform import IS_FREEBSD

    if IS_FREEBSD:
        from ev.platform.freebsd_process import lifetime

        return lifetime(pid, arguments.get("start_ticks"))
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    base = Path("/proc") / str(pid)
    try:
        owner = base.stat().st_uid
        # Foreign-user process lifecycle is outside this personal agent's scope.
        if owner != os.getuid():
            raise ValueError("Process is not owned by this desktop user")
        stat = (base / "stat").read_text(encoding="utf-8")
        end = stat.rfind(")")
        fields = stat[end + 2 :].split()
        if end < 0 or stat.split(" ", 1)[0] != str(pid) or len(fields) < 20:
            raise ValueError("Incomplete process identity observation")
        start_ticks, state = fields[19], fields[0]
        if not start_ticks.isdecimal():
            raise ValueError("Invalid process start time")
        expected = arguments.get("start_ticks")
        status = (
            "REPLACED"
            if expected and expected != start_ticks
            else "EXITED" if state in {"Z", "X"} else "RUNNING"
        )
    except (FileNotFoundError, ProcessLookupError):
        start_ticks, state, status = None, None, "ABSENT"
    return {
        "pid": pid,
        "boot_id": boot_id,
        "start_ticks": start_ticks,
        "kernel_state": state,
        "lifetime_status": status,
        "exit_code_known": False,
        "task_success_verified": False,
        "message": "Process lifetime only. Termination does not prove the requested task succeeded.",
    }


def register_process_lifetime_tools(registry):
    registry.register(
        ToolSpec(
            "system.process_lifetime",
            "SYSTEM",
            "Observe a same-user process PID, boot ID, start ticks and running/exited state without reading command lines or sending signals. Supply a previously observed start_ticks to detect PID reuse. Exit status is unknown: process disappearance is not task success.",
            Permission.SAFE,
            object_schema(
                {
                    "pid": {"type": "integer", "minimum": 1, "maximum": 4194304},
                    "start_ticks": {"type": "string", "pattern": "^[0-9]{1,24}$"},
                },
                ["pid"],
            ),
            process_lifetime,
            read_only=True,
        )
    )
