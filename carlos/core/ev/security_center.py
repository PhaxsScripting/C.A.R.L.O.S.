from __future__ import annotations

from ev.platform import executable as _platform_executable

import os
import json
import pwd
import re
import shutil
import socket
import stat
import subprocess
import time
from pathlib import Path
from typing import Any

from .paths import Paths


def _run(arguments: list[str], timeout: float = 8.0, maximum: int = 200_000) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        result = subprocess.run(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {
            "ok": False,
            "returncode": None,
            "stdout": "",
            "stderr": str(error),
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        }
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": result.stdout[:maximum],
        "stderr": result.stderr[:maximum],
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
    }


def _binding_scope(address: str) -> str:
    host = address.rsplit(":", 1)[0].strip("[]")
    if host in {"127.0.0.1", "::1", "localhost"}:
        return "LOCALHOST"
    if host in {"0.0.0.0", "::", "*"}:
        return "ALL_INTERFACES"
    return "INTERFACE"


class SecurityCenter:
    """Read-only local security inspection with evidence and confidence."""

    def __init__(self, paths: Paths, config: dict[str, Any]) -> None:
        from .platform import IS_FREEBSD
        from .platform.freebsd_security import NativeSecurity

        self._native = NativeSecurity(_run) if IS_FREEBSD else None
        self.paths = paths
        self.config = config
        self._updates_cache: dict[str, Any] | None = None

    def firewall(self) -> dict[str, Any]:
        from .platform import IS_FREEBSD

        if IS_FREEBSD:
            from .platform.system import firewall_report

            return firewall_report()
        services: list[dict[str, Any]] = []
        for name in ("nftables", "iptables", "firewalld", "ufw"):
            result = _run([_platform_executable("/sbin/rc-service"), name, "status"], timeout=3)
            combined = (result["stdout"] + result["stderr"]).strip()
            installed = not (
                "does not exist" in combined.casefold() or "not found" in combined.casefold()
            )
            if installed:
                services.append(
                    {
                        "name": name,
                        "running": result["returncode"] == 0,
                        "evidence": combined[-500:],
                    }
                )
        nft = (
            _run([_platform_executable("/usr/sbin/nft"), "list", "ruleset"], timeout=5)
            if Path(_platform_executable("/usr/sbin/nft")).exists()
            else _run([_platform_executable("/usr/bin/nft"), "list", "ruleset"], timeout=5)
        )
        readable = nft["ok"]
        runtime = self.firewall_runtime()
        rule_lines = [
            line.strip()
            for line in nft["stdout"].splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        active_services = [item["name"] for item in services if item["running"]]
        status = "ACTIVE" if active_services else "INACTIVE_OR_UNKNOWN"
        confidence = "HIGH" if services or readable else "MEDIUM"
        return {
            "status": status,
            "active_implementations": active_services,
            "services": services,
            "rules_readable_as_user": readable,
            "non_comment_rule_lines": len(rule_lines),
            "confidence": confidence,
            "runtime": runtime,
            "evidence": ["OpenRC rc-service status", "nft list ruleset", runtime["read_method"]],
            "limitations": (
                []
                if runtime["readable"]
                else [
                    "Kernel rules require administrator authorization; service status alone does not verify filtering policy"
                ]
            ),
        }

    def firewall_runtime(self, authorize: bool = False) -> dict[str, Any]:
        from .platform import IS_FREEBSD

        if IS_FREEBSD:
            from .platform.system import firewall_report

            return firewall_report()
        executable = next(
            (
                str(path)
                for path in (
                    Path(_platform_executable("/usr/bin/nft")),
                    Path(_platform_executable("/usr/sbin/nft")),
                )
                if path.is_file()
            ),
            "",
        )
        if not executable:
            return {
                "readable": False,
                "read_method": "unavailable",
                "message": "nft is not installed.",
            }
        command = [executable, "-j", "list", "ruleset"]
        result = _run(command, timeout=5)
        method = "unprivileged"
        if not result["ok"] and shutil.which("sudo"):
            result = _run([_platform_executable("/usr/bin/sudo"), "-n", *command], timeout=5)
            method = "existing_sudo_authorization"
        if not result["ok"] and authorize and shutil.which("pkexec"):
            result = _run([_platform_executable("/usr/bin/pkexec"), *command], timeout=90)
            method = "native_admin_dialog_read_only"
        if not result["ok"]:
            return {
                "readable": False,
                "verified": False,
                "read_method": method,
                "admin_authorization_required": True,
                "rules_changed": False,
                "message": "The firewall is protected by the OS. A read-only administrator check is needed to inspect its runtime rules.",
                "error": result["stderr"][:400],
            }
        try:
            records = json.loads(result["stdout"])["nftables"]
        except (ValueError, KeyError, TypeError):
            return {
                "readable": False,
                "verified": False,
                "read_method": method,
                "message": "Runtime rule output could not be parsed completely.",
            }
        chains = [item["chain"] for item in records if "chain" in item]
        rules = [item["rule"] for item in records if "rule" in item]
        return {
            "readable": True,
            "verified": True,
            "read_method": method,
            "admin_authorization_required": False,
            "rules_changed": False,
            "table_count": sum("table" in item for item in records),
            "rule_count": len(rules),
            "chains": [
                {
                    key: chain[key]
                    for key in ("family", "table", "name", "hook", "policy")
                    if key in chain
                }
                for chain in chains
            ],
            "message": f"Read the active kernel firewall: {len(rules)} rules across {len(chains)} chains. No rules changed.",
        }

    def sockets(self) -> dict[str, Any]:
        if self._native is not None:
            return self._native.sockets()
        listening_result = (
            _run([_platform_executable("/usr/sbin/ss"), "-H", "-lntup"], timeout=5)
            if Path(_platform_executable("/usr/sbin/ss")).exists()
            else _run([_platform_executable("/usr/bin/ss"), "-H", "-lntup"], timeout=5)
        )
        established_result = (
            _run(
                [_platform_executable("/usr/sbin/ss"), "-H", "-ntup", "state", "established"],
                timeout=5,
            )
            if Path(_platform_executable("/usr/sbin/ss")).exists()
            else _run(
                [_platform_executable("/usr/bin/ss"), "-H", "-ntup", "state", "established"],
                timeout=5,
            )
        )
        listeners: list[dict[str, Any]] = []
        for line in listening_result["stdout"].splitlines():
            fields = line.split()
            if len(fields) < 5:
                continue
            protocol = fields[0]
            local = fields[4] if protocol.startswith("tcp") else fields[4]
            process_match = re.search(r'users:\(\("([^"]+)",pid=(\d+)', line)
            listeners.append(
                {
                    "protocol": protocol,
                    "local": local,
                    "scope": _binding_scope(local),
                    "process": process_match.group(1) if process_match else "UNKNOWN",
                    "pid": int(process_match.group(2)) if process_match else None,
                }
            )
        exposed = [item for item in listeners if item["scope"] != "LOCALHOST"]
        established_count = len(
            [line for line in established_result["stdout"].splitlines() if line.strip()]
        )
        return {
            "listeners": listeners,
            "listener_count": len(listeners),
            "network_accessible_count": len(exposed),
            "localhost_only_count": len(listeners) - len(exposed),
            "established_connection_count": established_count,
            "confidence": "HIGH" if listening_result["ok"] else "LOW",
            "evidence": ["ss -lntup", "ss state established"],
            "limitations": (
                []
                if listening_result["ok"]
                else [listening_result["stderr"] or "Socket inspection failed"]
            ),
        }

    def ssh(self) -> dict[str, Any]:
        if self._native is not None:
            return self._native.ssh()
        service = _run([_platform_executable("/sbin/rc-service"), "sshd", "status"], timeout=3)
        text = (service["stdout"] + service["stderr"]).strip()
        exists = not ("does not exist" in text.casefold() or "not found" in text.casefold())
        config_path = Path("/etc/ssh/sshd_config")
        port = 22
        if config_path.is_file() and os.access(config_path, os.R_OK):
            try:
                for line in config_path.read_text(errors="replace").splitlines():
                    match = re.match(r"\s*Port\s+(\d+)", line, re.IGNORECASE)
                    if match and not line.lstrip().startswith("#"):
                        port = int(match.group(1))
            except OSError:
                pass
        return {
            "installed": exists,
            "running": exists and service["returncode"] == 0,
            "configured_port": port,
            "evidence": ["OpenRC rc-service sshd status", str(config_path)],
            "detail": text[-500:],
            "confidence": "HIGH" if exists else "MEDIUM",
        }

    def startup(self) -> dict[str, Any]:
        roots = [Path.home() / ".config/autostart", Path("/etc/xdg/autostart")]
        if self._native is not None:
            roots.append(Path("/usr/local/etc/xdg/autostart"))
        entries: list[dict[str, Any]] = []
        for root in roots:
            if not root.is_dir():
                continue
            for path in sorted(root.glob("*.desktop")):
                try:
                    mode = stat.S_IMODE(path.stat().st_mode)
                    content = path.read_text(errors="replace")
                except OSError:
                    continue
                name_match = re.search(r"^Name=(.+)$", content, re.MULTILINE)
                hidden = bool(
                    re.search(r"^Hidden\s*=\s*true\s*$", content, re.MULTILINE | re.IGNORECASE)
                )
                entries.append(
                    {
                        "name": name_match.group(1).strip() if name_match else path.stem,
                        "path": str(path),
                        "scope": "USER" if root.is_relative_to(Path.home()) else "SYSTEM",
                        "enabled": not hidden,
                        "mode": oct(mode),
                    }
                )
        shell_files = [
            path
            for path in (
                Path.home() / ".profile",
                Path.home() / ".bash_profile",
                Path.home() / ".bashrc",
                Path.home() / ".zshrc",
            )
            if path.exists()
        ]
        return {
            "entries": entries,
            "enabled_count": sum(bool(item["enabled"]) for item in entries),
            "shell_startup_files": [str(path) for path in shell_files],
            "evidence": [str(root) for root in roots],
            "confidence": "HIGH",
        }

    def login_activity(self) -> dict[str, Any]:
        if self._native is not None:
            return self._native.login_activity()
        result = _run(
            [_platform_executable("/usr/bin/last"), "-n", "20", "--time-format", "iso"],
            timeout=5,
            maximum=20_000,
        )
        lines = [
            line
            for line in result["stdout"].splitlines()
            if line.strip() and not line.startswith("wtmp begins")
        ]
        sessions = []
        for line in lines[:20]:
            fields = line.split()
            sessions.append(
                {
                    "user": fields[0] if fields else "",
                    "terminal": fields[1] if len(fields) > 1 else "",
                    "summary": line[:300],
                }
            )
        return {
            "recent_sessions": sessions,
            "count": len(sessions),
            "evidence": ["last -n 20 --time-format iso"],
            "confidence": "HIGH" if result["ok"] else "LOW",
            "limitations": [] if result["ok"] else [result["stderr"]],
        }

    def updates(self, refresh: bool = False) -> dict[str, Any]:
        if self._native is not None:
            return self._native.updates(refresh)
        if self._updates_cache is not None and not refresh:
            return dict(self._updates_cache)
        glsa = shutil.which("glsa-check")
        if not glsa:
            return {
                "status": "UNAVAILABLE",
                "confidence": "HIGH",
                "evidence": ["command discovery"],
                "limitations": ["glsa-check is not installed; no update claim was made"],
            }
        if not refresh:
            return {
                "status": "NOT_SCANNED",
                "confidence": "HIGH",
                "evidence": ["glsa-check executable discovery"],
                "limitations": [
                    "The potentially slow GLSA scan runs only when explicitly requested"
                ],
            }
        result = _run([glsa, "-l"], timeout=20, maximum=100_000)
        unresolved = [
            line.strip()
            for line in result["stdout"].splitlines()
            if line.strip() and not line.startswith("[")
        ]
        self._updates_cache = {
            "status": "REVIEW" if unresolved else "NO_UNRESOLVED_GLSA_REPORTED",
            "unresolved": unresolved[:100],
            "confidence": "MEDIUM",
            "evidence": ["glsa-check -l"],
            "limitations": ["This checks GLSA advisories, not every possible package update"],
            "duration_ms": result["duration_ms"],
        }
        return dict(self._updates_cache)

    def ev_security(self) -> dict[str, Any]:
        targets = {
            "runtime_directory": self.paths.runtime_dir,
            "ipc_socket": self.paths.socket,
            "config": self.paths.config_file,
            "database": self.paths.database,
            "provider_environment": self.paths.config_dir / "provider.env",
        }
        files: list[dict[str, Any]] = []
        findings: list[dict[str, Any]] = []
        uid = os.getuid()
        for name, path in targets.items():
            if not path.exists():
                files.append({"name": name, "path": str(path), "exists": False})
                continue
            info = path.stat()
            mode = stat.S_IMODE(info.st_mode)
            item = {
                "name": name,
                "path": str(path),
                "exists": True,
                "owner_uid": info.st_uid,
                "owner": pwd.getpwuid(info.st_uid).pw_name,
                "mode": oct(mode),
                "is_socket": stat.S_ISSOCK(info.st_mode),
            }
            files.append(item)
            if info.st_uid != uid:
                findings.append(
                    {
                        "severity": "HIGH",
                        "kind": "OWNER",
                        "target": name,
                        "detail": "Sensitive E.V. path is not owned by the current user",
                    }
                )
            if mode & 0o077:
                findings.append(
                    {
                        "severity": (
                            "HIGH"
                            if name in {"provider_environment", "database", "ipc_socket"}
                            else "MEDIUM"
                        ),
                        "kind": "PERMISSIONS",
                        "target": name,
                        "detail": f"Mode {oct(mode)} grants group/other access",
                    }
                )
        return {
            "files": files,
            "findings": findings,
            "ipc_local_only": self.paths.socket.exists()
            and stat.S_ISSOCK(self.paths.socket.stat().st_mode),
            "api_key_storage": (
                "private provider.env allowlist"
                if (self.paths.config_dir / "provider.env").exists()
                else "no provider.env present"
            ),
            "approval_mode": str(
                self.config.get("security", {}).get("approval_mode", "codex_only")
            ),
            "approval_scope": ["development.coding_agent_execute"],
            "evidence": [
                "lstat owner and permission bits",
                "Unix-domain socket type",
                "E.V. runtime config",
            ],
            "confidence": "HIGH",
        }

    def overview(self) -> dict[str, Any]:
        started = time.perf_counter()
        firewall = self.firewall()
        network = self.sockets()
        ssh = self.ssh()
        startup = self.startup()
        logins = self.login_activity()
        updates = self.updates()
        ev = self.ev_security()
        findings: list[dict[str, Any]] = list(ev["findings"])
        if firewall["status"] != "ACTIVE":
            findings.append(
                {
                    "severity": "MEDIUM",
                    "kind": "FIREWALL",
                    "detail": "No active firewall service was confirmed",
                    "confidence": firewall["confidence"],
                }
            )
        if network["network_accessible_count"]:
            findings.append(
                {
                    "severity": "INFO",
                    "kind": "NETWORK_EXPOSURE",
                    "detail": f"{network['network_accessible_count']} listeners are bound beyond localhost",
                    "confidence": network["confidence"],
                }
            )
        if ssh["running"]:
            findings.append(
                {
                    "severity": "INFO",
                    "kind": "SSH",
                    "detail": f"SSH is running on configured port {ssh['configured_port']}",
                    "confidence": ssh["confidence"],
                }
            )
        rank = {"HIGH": 3, "MEDIUM": 2, "INFO": 1}
        highest = max((rank.get(item.get("severity", "INFO"), 0) for item in findings), default=0)
        status = {3: "ACTION_RECOMMENDED", 2: "REVIEW", 1: "OBSERVED", 0: "NO_FINDINGS"}[highest]
        return {
            "status": status,
            "derived_from": {
                "high": sum(item.get("severity") == "HIGH" for item in findings),
                "medium": sum(item.get("severity") == "MEDIUM" for item in findings),
                "info": sum(item.get("severity") == "INFO" for item in findings),
            },
            "findings": findings,
            "firewall": firewall,
            "network": network,
            "ssh": ssh,
            "startup": startup,
            "login_activity": logins,
            "updates": updates,
            "ev": ev,
            "host": socket.gethostname(),
            "local_only": True,
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        }
