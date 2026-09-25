"""Native listener ownership. Missing or ambiguous observations fail closed."""

import os
import subprocess
from .system import process_identity


def owns_listener(pid: int, host: str, port: int) -> bool:
    if host not in {"127.0.0.1", "::1"} or not 1 <= port <= 65535:
        return False
    before = process_identity(pid)
    if not before or before["uid"] != os.getuid():
        return False
    try:
        result = subprocess.run(
            ["/usr/bin/sockstat", "-46lnq", "-P", "tcp", "-p", str(port)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if result.returncode or result.stderr.strip():
            return False
        rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
        if not rows:
            return False
        # USER COMMAND PID FD PROTO LOCAL FOREIGN. Never accept wildcard binds,
        # listeners shared with another PID, or unexpected output columns.
        for row in rows:
            if len(row) != 7 or int(row[0]) != os.getuid() or int(row[2]) != pid:
                return False
            if row[4] not in {"tcp4", "tcp6"} or row[5] not in {
                f"{host}:{port}",
                f"[{host}]:{port}",
            }:
                return False
        return process_identity(pid) == before
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False
