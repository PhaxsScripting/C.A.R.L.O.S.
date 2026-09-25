"""Native process observations through psutil's FreeBSD kinfo_proc support."""

import os
import psutil
from .system import boot_id


def lifetime(pid, expected=None):
    start = None
    state = None
    status = "ABSENT"
    try:
        p = psutil.Process(pid)
        first = p.create_time()
        if p.uids().real != os.getuid():
            raise ValueError("Process is not owned by this desktop user")
        state = p.status()
        start = str(int(first * 1_000_000))
        if psutil.Process(pid).create_time() != first:
            status = "REPLACED"
        else:
            status = (
                "REPLACED"
                if expected and expected != start
                else "EXITED" if state == psutil.STATUS_ZOMBIE else "RUNNING"
            )
    except psutil.NoSuchProcess:
        pass
    return {
        "pid": pid,
        "boot_id": boot_id(),
        "start_ticks": start,
        "kernel_state": state,
        "lifetime_status": status,
        "exit_code_known": False,
        "task_success_verified": False,
        "message": "Native process lifetime only; disappearance does not prove task success.",
    }
