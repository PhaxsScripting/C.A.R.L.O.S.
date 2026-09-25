"""Read-only FreeBSD security observations in the shared UI's existing schema."""

import os
from pathlib import Path
import re


class NativeSecurity:
    def __init__(self, run):
        self.run = run

    def sockets(self):
        result = self.run(["/usr/bin/sockstat", "-46lnq"], timeout=5)
        connected = self.run(["/usr/bin/netstat", "-an", "-p", "tcp"], timeout=5)
        listeners = []
        unparsed = 0
        for line in result["stdout"].splitlines():
            fields = line.split()
            if len(fields) != 7 or not fields[2].isdigit():
                unparsed += 1
                continue
            local = fields[5]
            host = local.rsplit(":", 1)[0].strip("[]")
            scope = (
                "LOCALHOST"
                if host in {"127.0.0.1", "::1"}
                else "ALL_INTERFACES" if host in {"*", "0.0.0.0", "::"} else "INTERFACE"
            )
            listeners.append(
                {
                    "protocol": fields[4],
                    "local": local,
                    "scope": scope,
                    "process": fields[1],
                    "pid": int(fields[2]),
                }
            )
        exposed = sum(p["scope"] != "LOCALHOST" for p in listeners)
        return {
            "listeners": listeners,
            "listener_count": len(listeners),
            "network_accessible_count": exposed,
            "localhost_only_count": len(listeners) - exposed,
            "established_connection_count": (
                sum("ESTABLISHED" in l for l in connected["stdout"].splitlines())
                if connected["ok"]
                else None
            ),
            "confidence": "HIGH" if result["ok"] and not unparsed else "LOW",
            "evidence": ["sockstat -46lnq", "netstat -an -p tcp"],
            "limitations": (
                []
                if result["ok"] and not unparsed
                else ["Some socket observations are unavailable; do not infer no exposure"]
            ),
        }

    def ssh(self):
        result = self.run(["/usr/sbin/service", "sshd", "onestatus"], timeout=3)
        binary = Path("/usr/sbin/sshd")
        return {
            "installed": binary.is_file(),
            "running": result["ok"],
            "configured_port": None,
            "evidence": ["service sshd onestatus", "live sockets"],
            "detail": (result["stdout"] + result["stderr"])[-500:],
            "confidence": "HIGH" if result["ok"] else "LOW",
            "limitations": [
                "Effective SSH Match/Include configuration is not inferred from a single file; use live listener observations."
            ],
        }

    def login_activity(self):
        result = self.run(["/usr/bin/last", "-n", "20"], timeout=5, maximum=20000)
        sessions = []
        for line in result["stdout"].splitlines():
            fields = line.split()
            if len(fields) < 2 or line.startswith(("wtmp", "utx.log")):
                continue
            sessions.append({"user": fields[0], "terminal": fields[1], "summary": line[:300]})
        return {
            "recent_sessions": sessions[:20],
            "count": min(len(sessions), 20),
            "evidence": ["FreeBSD last -n 20"],
            "confidence": "HIGH" if result["ok"] else "LOW",
            "limitations": [] if result["ok"] else [result["stderr"]],
        }

    def updates(self, refresh=False):
        if not refresh:
            return {
                "status": "NOT_SCANNED",
                "confidence": "HIGH",
                "evidence": ["FreeBSD package audit adapter"],
                "limitations": [
                    "Explicit refresh reads the existing vulnerability database; it never downloads or installs updates."
                ],
            }
        database = Path("/var/db/pkg/vuln.xml")
        if not database.is_file() or not os.access(database, os.R_OK):
            return {
                "status": "UNAVAILABLE",
                "confidence": "HIGH",
                "evidence": [str(database)],
                "limitations": [
                    "No readable local vulnerability database. No claim of security currency is made."
                ],
            }
        result = self.run(
            ["/usr/local/sbin/pkg", "audit", "-f", str(database)], timeout=20, maximum=100000
        )
        return {
            "status": "NO_LOCAL_ADVISORIES_REPORTED" if result["ok"] else "REVIEW",
            "unresolved": result["stdout"].splitlines()[:100],
            "confidence": "MEDIUM",
            "evidence": ["pkg audit using existing local database"],
            "limitations": [
                "Local vulnerability data may be stale; this does not check base-system advisories or every possible update.",
                result["stderr"],
            ],
            "duration_ms": result["duration_ms"],
        }
