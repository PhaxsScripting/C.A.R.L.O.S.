from __future__ import annotations

from ev.platform import executable as _platform_executable

import json
import os
import re
import signal
import selectors
import threading
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable

EventSink = Callable[[str, dict[str, Any], str], None]


class CodingAgentGateway:
    """Approval-gated Codex runner with an isolated Git-worktree boundary."""

    def __init__(
        self,
        allowed_roots: list[str],
        state_root: Path | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        self.allowed_roots = [Path(item).expanduser().resolve() for item in allowed_roots]
        self.state_root = state_root.resolve() if state_root is not None else None
        self.event_sink = event_sink
        self._proposals: dict[str, dict[str, Any]] = {}
        self._running: dict[str, threading.Event] = {}
        self._running_lock = threading.RLock()
        self._status_cache: tuple[float, dict[str, Any]] | None = None
        if self.state_root is not None:
            self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.state_root, 0o700)
            self._load_existing()

    @staticmethod
    def _codex_executable() -> str | None:
        installed = Path.home() / ".local/bin/codex"
        if installed.is_file() or installed.is_symlink():
            return str(installed)
        return shutil.which("codex")

    def status(self, refresh: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if not refresh and self._status_cache is not None and now - self._status_cache[0] < 10:
            return dict(self._status_cache[1])
        executable = self._codex_executable()
        authenticated = False
        version = ""
        reason = "No codex executable is discoverable in the E.V. service PATH"
        if executable:
            version_result = self._run([executable, "--version"], timeout=5)
            version = version_result["stdout"].strip() if version_result["returncode"] == 0 else ""
            auth_result = self._run([executable, "login", "status"], timeout=5)
            # The standalone CLI currently writes its human-readable login
            # status to stderr.  Treat both captured streams as status text;
            # the exit code remains the authority for success.
            auth_text = f"{auth_result['stdout']}\n{auth_result['stderr']}".casefold()
            authenticated = auth_result["returncode"] == 0 and "logged in" in auth_text
            reason = (
                "Codex CLI and saved login are ready"
                if authenticated
                else "Codex CLI is installed but is not logged in"
            )
        result = {
            "available": executable is not None and authenticated,
            "installed": executable is not None,
            "authenticated": authenticated,
            "executable": executable or "",
            "version": version,
            "mode": (
                "approval_gated_isolated_worktree"
                if executable and authenticated
                else "proposal_only"
            ),
            "writes_without_approval": False,
            "live_checkout_writes": False,
            "sandbox": "workspace-write",
            "user_config_loaded": False,
            "exec_policy_rules_loaded": False,
            "project_instructions": "Codex loads applicable project instructions",
            "may_change_permissions": False,
            "network_exposure": "Codex service connection only; model-generated network access not granted",
            "reason": reason,
            "tasks": [
                self._task_summary(item)
                for item in sorted(
                    self._proposals.values(), key=lambda row: row.get("created_epoch", 0)
                )[-12:]
            ],
        }
        self._status_cache = (now, result)
        return dict(result)

    def _project(self, value: str) -> Path:
        path = Path(value).expanduser().resolve()
        if not any(path == root or path.is_relative_to(root) for root in self.allowed_roots):
            raise ValueError("project is outside E.V.'s configured allowed roots")
        if not path.is_dir():
            raise ValueError("project directory does not exist")
        result = self._git(path, "rev-parse", "--show-toplevel", timeout=10)
        if result["returncode"] != 0:
            raise ValueError("project is not a Git repository")
        root = Path(result["stdout"].strip()).resolve()
        if not any(
            root == allowed or root.is_relative_to(allowed) for allowed in self.allowed_roots
        ):
            raise ValueError("Git repository root is outside E.V.'s configured allowed roots")
        return root

    def propose(self, request: str, project: str, diagnostics: str = "") -> dict[str, Any]:
        clean = request.strip()
        if not clean or len(clean) > 4000:
            raise ValueError("coding request must be 1-4000 characters")
        if len(diagnostics) > 20_000:
            raise ValueError("diagnostics must be at most 20000 characters")
        root = self._project(project)
        status_result = self._git(root, "status", "--porcelain=v1", timeout=10)
        head_result = self._git(root, "rev-parse", "HEAD", timeout=10)
        if status_result["returncode"] != 0 or head_result["returncode"] != 0:
            raise ValueError("Git checkpoint could not be inspected")
        clean_checkout = not status_result["stdout"].strip()
        agent = self.status(refresh=True)
        proposal_id = uuid.uuid4().hex
        proposal = {
            "proposal_id": proposal_id,
            "request": clean,
            "diagnostics": self._redact_diagnostics(diagnostics.strip()),
            "project": str(root),
            "agent": {key: value for key, value in agent.items() if key != "tasks"},
            "status": "READY_FOR_REVIEW" if agent["available"] and clean_checkout else "BLOCKED",
            "created_epoch": time.time(),
            "git_status": status_result["stdout"].strip() or "CLEAN",
            "base_commit": head_result["stdout"].strip(),
            "checkpoint_possible": clean_checkout,
            "approval_required": True,
            "permission_class": "HIGH",
            "planned_phases": [
                "revalidate the exact clean Git checkpoint",
                "create a separate Git branch and worktree",
                "run Codex with workspace-write and isolated configuration",
                "capture JSONL status without exposing hidden reasoning",
                "show changed files plus dependency, permission, network, and integration risks",
                "run fixed validation commands",
                "commit only a passing worktree result",
                "require a separate reviewed deployment action",
            ],
            "executed": False,
            "capability_gap": (
                None
                if agent["available"]
                else {
                    "type": "MISSING_DEPENDENCY",
                    "required": "authenticated Codex CLI",
                    "reason": agent["reason"],
                    "requires_user_approval": True,
                }
            ),
        }
        self._proposals[proposal_id] = proposal
        self._save(proposal)
        self._status_cache = None
        self._emit("coding.proposed", proposal, proposal_id)
        return self._public(proposal)

    def cancel(self, proposal_id: str) -> dict[str, Any]:
        with self._running_lock:
            event = self._running.get(proposal_id)
            if event is not None:
                event.set()
        return {
            "proposal_id": proposal_id,
            "cancel_requested": event is not None,
            "status": "CANCELLING" if event is not None else "NOT_RUNNING",
            "verified": event is None,
            "scope": "Cancellation request; completion is reported by the job",
        }

    def cancel_all(self):
        with self._running_lock:
            ids = list(self._running)
            for event in self._running.values():
                event.set()
        return {"cancel_requested": ids}

    def execute(self, proposal_id: str, timeout_seconds: int = 1200) -> dict[str, Any]:
        with self._running_lock:
            if proposal_id in self._running:
                raise ValueError("Coding task is already running")
            self._running[proposal_id] = threading.Event()
        try:
            return self._execute_inner(proposal_id, timeout_seconds)
        except Exception as error:
            proposal = self._proposals.get(proposal_id, {})
            if proposal.get("status") == "RUNNING":
                proposal.update(
                    status="FAILED",
                    failure=self._redact_diagnostics(str(error))[:500],
                    completed_epoch=time.time(),
                )
                self._save(proposal)
                self._status_cache = None
                self._emit("coding.failed", self._public(proposal), proposal_id)
            raise
        finally:
            with self._running_lock:
                self._running.pop(proposal_id, None)

    def _execute_inner(self, proposal_id: str, timeout_seconds: int = 1200) -> dict[str, Any]:
        proposal = self._proposal(proposal_id)
        if proposal.get("executed"):
            raise ValueError("coding proposal has already been executed")
        if proposal.get("status") != "READY_FOR_REVIEW":
            raise ValueError("coding proposal is not ready for execution")
        root = self._project(str(proposal["project"]))
        status = self.status(refresh=True)
        if not status["available"]:
            raise RuntimeError(status["reason"])
        live_head = self._git(root, "rev-parse", "HEAD", timeout=10)
        live_status = self._git(root, "status", "--porcelain=v1", timeout=10)
        if live_head["stdout"].strip() != proposal["base_commit"] or live_status["stdout"].strip():
            raise RuntimeError("live repository changed after proposal; create a new proposal")
        if self.state_root is None:
            raise RuntimeError("coding task state directory is not configured")

        task_dir = self.state_root / proposal_id
        worktree = task_dir / "worktree"
        branch = f"ev-coding-{proposal_id[:12]}"
        task_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(task_dir, 0o700)
        if worktree.exists():
            raise RuntimeError("coding worktree already exists")
        added = self._git(
            root,
            "worktree",
            "add",
            "-b",
            branch,
            str(worktree),
            str(proposal["base_commit"]),
            timeout=30,
        )
        if added["returncode"] != 0:
            raise RuntimeError(added["stderr"].strip() or "failed to create isolated Git worktree")

        proposal.update(
            {
                "status": "RUNNING",
                "executed": True,
                "branch": branch,
                "worktree": str(worktree),
                "started_epoch": time.time(),
            }
        )
        self._save(proposal)
        self._status_cache = None
        self._emit(
            "coding.started",
            {
                "proposal_id": proposal_id,
                "project": str(root),
                "started_epoch": proposal["started_epoch"],
                "branch": branch,
                "base_commit": proposal["base_commit"],
            },
            proposal_id,
        )

        command = [
            str(status["executable"]),
            "exec",
            "--sandbox",
            "workspace-write",
            "--ephemeral",
            "--json",
            "--color",
            "never",
            "--ignore-user-config",
            "--ignore-rules",
            "-C",
            str(worktree),
            self._prompt(proposal),
        ]
        started = time.perf_counter()
        if self._running[proposal_id].is_set():
            proposal.update({"status": "CANCELLED", "completed_epoch": time.time()})
            self._save(proposal)
            self._emit("coding.cancelled", self._public(proposal), proposal_id)
            return self._public(proposal)
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self._clean_environment(),
            start_new_session=True,
        )
        stdout, stderr, stopped = self._collect_agent(
            process, proposal_id, max(60, min(int(timeout_seconds), 1800))
        )
        if stopped:
            proposal.update(
                {
                    "status": "CANCELLED" if stopped == "cancelled" else "FAILED",
                    "failure": stopped,
                    "completed_epoch": time.time(),
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                }
            )
            self._save(proposal)
            self._status_cache = None
            self._emit(
                "coding.cancelled" if stopped == "cancelled" else "coding.failed",
                self._public(proposal),
                proposal_id,
            )
            return self._public(proposal)

        events = self._parse_jsonl(stdout)
        proposal["codex"] = {
            "returncode": process.returncode,
            "event_counts": events["event_counts"],
            "thread_id": events["thread_id"],
            "final_message": events["final_message"][-20_000:],
            "stderr": stderr[-20_000:],
        }
        proposal["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
        if process.returncode != 0:
            proposal.update({"status": "FAILED", "failure": "Codex exited unsuccessfully"})
            self._save(proposal)
            self._status_cache = None
            self._emit("coding.failed", self._public(proposal), proposal_id)
            return self._public(proposal)

        if self._running[proposal_id].is_set():
            proposal.update(status="CANCELLED", completed_epoch=time.time())
            self._save(proposal)
            self._emit("coding.cancelled", self._public(proposal), proposal_id)
            return self._public(proposal)
        assessment = self._assess_changes(worktree, task_dir)
        validations = self._validate(worktree)
        proposal["change_review"] = assessment
        proposal["tests"] = validations
        if self._running[proposal_id].is_set():
            proposal.update(status="CANCELLED", failure="Cancelled before review commit")
        elif not assessment["changed_files"]:
            proposal.update(
                {"status": "NO_CHANGES", "failure": "Codex produced no repository changes"}
            )
        elif not all(item["passed"] for item in validations):
            proposal.update(
                {"status": "VALIDATION_FAILED", "failure": "One or more fixed validations failed"}
            )
        else:
            committed = self._commit_result(worktree, proposal_id)
            if committed["returncode"] == 0:
                commit_id = self._git(worktree, "rev-parse", "HEAD", timeout=10)["stdout"].strip()
                proposal.update(
                    {
                        "status": "VALIDATED_AWAITING_DEPLOYMENT_REVIEW",
                        "commit_id": commit_id,
                        "deployment_approved": False,
                        "rollback": f"git -C {root} revert {commit_id}",
                    }
                )
            else:
                proposal.update(
                    {
                        "status": "FAILED",
                        "failure": committed["stderr"].strip()
                        or "failed to commit validated result",
                    }
                )
        proposal["completed_epoch"] = time.time()
        self._save(proposal)
        self._status_cache = None
        self._emit("coding.completed", self._public(proposal), proposal_id)
        return self._public(proposal)

    def _collect_agent(self, process, proposal_id, timeout):
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        pending = bytearray()
        changed_files = set()
        stopped = ""
        deadline = time.monotonic() + timeout
        cancel = self._running[proposal_id]
        with selectors.DefaultSelector() as selector:
            for name, stream in [("stdout", process.stdout), ("stderr", process.stderr)]:
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            try:
                while selector.get_map():
                    if cancel.is_set() or time.monotonic() >= deadline:
                        stopped = "cancelled" if cancel.is_set() else "Codex execution timed out"
                        break
                    for key, _ in selector.select(0.1):
                        chunk = os.read(key.fileobj.fileno(), 8192)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        buffers[key.data].extend(chunk)
                        if len(buffers[key.data]) > 2 * 1024 * 1024:
                            stopped = "Codex output exceeded the bounded capture limit"
                            break
                        if key.data == "stdout":
                            pending.extend(chunk)
                            while b"\n" in pending:
                                line, _, remaining = pending.partition(b"\n")
                                pending = bytearray(remaining)
                                if len(line) > 65536:
                                    continue
                                try:
                                    event = json.loads(line)
                                except (ValueError, UnicodeError):
                                    continue
                                if not isinstance(event, dict):
                                    continue
                                item = event.get("item") or {}
                                if not isinstance(item, dict):
                                    continue
                                kind = item.get("type", "")
                                if kind in {"agent_message", "command_execution", "file_change"}:
                                    payload = {
                                        "proposal_id": proposal_id,
                                        "event": str(event.get("type", ""))[:80],
                                        "kind": kind,
                                    }
                                    if kind == "file_change" and isinstance(
                                        item.get("changes"), list
                                    ):
                                        for change in item["changes"][:1000]:
                                            if isinstance(change, dict) and isinstance(
                                                change.get("path"), str
                                            ):
                                                changed_files.add(change["path"][:4096])
                                        payload["files_changed"] = len(changed_files)
                                    if kind == "agent_message":
                                        payload["message"] = self._redact_diagnostics(
                                            str(item.get("text", ""))[:2000]
                                        )
                                    self._emit("coding.progress", payload, proposal_id)
                            if len(pending) > 65536:
                                stopped = "Codex event exceeded the protocol limit"
                                break
                    if stopped:
                        break
                if stopped:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    stopped = stopped or "Codex did not exit after closing its output"
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=3)
            finally:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=3)
                process.stdout.close()
                process.stderr.close()
        return (
            buffers["stdout"].decode(errors="replace"),
            buffers["stderr"].decode(errors="replace"),
            stopped,
        )

    def result(self, proposal_id: str) -> dict[str, Any]:
        return self._public(self._proposal(proposal_id))

    def _assess_changes(self, worktree: Path, task_dir: Path) -> dict[str, Any]:
        status = self._git(worktree, "status", "--porcelain=v1", timeout=10)
        changed_files: list[str] = []
        for line in status["stdout"].splitlines():
            value = line[3:].strip()
            if " -> " in value:
                value = value.split(" -> ", 1)[1]
            if value:
                changed_files.append(value)
        diff = self._git(worktree, "diff", "--no-ext-diff", "--binary", timeout=30)["stdout"]
        patch_path = task_dir / "changes.patch"
        patch_path.write_text(diff, encoding="utf-8")
        os.chmod(patch_path, 0o600)
        dependency_names = {
            "requirements.txt",
            "pyproject.toml",
            "poetry.lock",
            "Pipfile",
            "package.json",
            "package-lock.json",
            "Cargo.toml",
            "Cargo.lock",
            "go.mod",
            "go.sum",
            "CMakeLists.txt",
            "meson.build",
            "build.gradle",
            "settings.gradle",
        }
        dependency_changes = [name for name in changed_files if Path(name).name in dependency_names]
        permission_changes = [
            name
            for name in changed_files
            if name in {"core/ev/permissions.py", "core/ev/tools/builtin.py", "docs/SECURITY.md"}
        ]
        integration_changes = [
            name for name in changed_files if name.startswith(("scripts/", "packaging/", "config/"))
        ]
        additions = "\n".join(
            line[1:]
            for line in diff.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        network_markers = sorted(
            set(
                re.findall(
                    r"\b(?:socket|listen|http|https|urllib|requests|aiohttp|network)\b",
                    additions,
                    re.IGNORECASE,
                )
            )
        )
        privileged_markers = sorted(
            set(re.findall(r"\b(?:sudo|pkexec|setuid|root|privileged)\b", additions, re.IGNORECASE))
        )
        return {
            "changed_files": sorted(set(changed_files)),
            "patch_path": str(patch_path),
            "patch_preview": diff[:60_000],
            "patch_truncated": len(diff) > 60_000,
            "dependency_changes": dependency_changes,
            "permission_manifest_changes": permission_changes,
            "system_integration_changes": integration_changes,
            "network_markers_added": network_markers,
            "privileged_markers_added": privileged_markers,
            "requires_security_review": bool(
                dependency_changes
                or permission_changes
                or integration_changes
                or network_markers
                or privileged_markers
            ),
        }

    def _validate(self, worktree: Path) -> list[dict[str, Any]]:
        commands: list[tuple[str, list[str], int]] = [
            (
                "git diff --check",
                [_platform_executable("/usr/bin/git"), "-C", str(worktree), "diff", "--check"],
                30,
            )
        ]
        if (worktree / "core/ev").is_dir():
            commands.append(
                (
                    "python compileall",
                    [_platform_executable("/usr/bin/python3"), "-m", "compileall", "-q", "core"],
                    120,
                )
            )
        if (worktree / "tests").is_dir() and (worktree / "core/ev").is_dir():
            commands.append(
                (
                    "Python unit suite",
                    [
                        _platform_executable("/usr/bin/python3"),
                        "-m",
                        "unittest",
                        "discover",
                        "-s",
                        "tests",
                    ],
                    600,
                )
            )
        results: list[dict[str, Any]] = []
        for label, command, timeout in commands:
            result = self._run(
                command, cwd=worktree, timeout=timeout, environment=self._clean_environment()
            )
            results.append(
                {
                    "name": label,
                    "passed": result["returncode"] == 0,
                    "returncode": result["returncode"],
                    "output": (result["stdout"] + result["stderr"])[-20_000:],
                }
            )
        return results

    def _commit_result(self, worktree: Path, proposal_id: str) -> dict[str, Any]:
        added = self._git(worktree, "add", "--all", timeout=30)
        if added["returncode"] != 0:
            return added
        return self._git(
            worktree,
            "-c",
            "user.name=E.V. Coding Agent",
            "-c",
            "user.email=ev-coding-agent@localhost",
            "commit",
            "-m",
            f"feat: implement E.V. coding task {proposal_id[:12]}",
            timeout=60,
        )

    @staticmethod
    def _prompt(proposal: dict[str, Any]) -> str:
        diagnostics = str(proposal.get("diagnostics", "")).strip()
        return "\n".join(
            [
                "Implement one focused change in this isolated Git worktree.",
                f"Task: {proposal['request']}",
                f"Diagnostics: {diagnostics or 'None supplied.'}",
                "Constraints:",
                "- Do not install packages, access credentials, modify system configuration, or expose a network service.",
                "- Do not commit, deploy, or change files outside this worktree.",
                "- Preserve existing behavior outside the requested change.",
                "- Add or update a focused regression test when practical.",
                "- Run relevant tests and leave the working tree changes for the supervising gateway to inspect.",
                "Finish with a concise summary and the tests you ran.",
            ]
        )

    @staticmethod
    def _parse_jsonl(raw: str) -> dict[str, Any]:
        counts: dict[str, int] = {}
        thread_id = ""
        messages: list[str] = []
        for line in raw.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_type = str(event.get("type", "unknown"))
            counts[event_type] = counts.get(event_type, 0) + 1
            if event_type == "thread.started":
                thread_id = str(event.get("thread_id", ""))
            item = event.get("item", {})
            if (
                isinstance(item, dict)
                and item.get("type") == "agent_message"
                and isinstance(item.get("text"), str)
            ):
                messages.append(item["text"])
        return {
            "event_counts": counts,
            "thread_id": thread_id,
            "final_message": messages[-1] if messages else "",
        }

    @staticmethod
    def _clean_environment() -> dict[str, str]:
        allowed = {
            "HOME",
            "USER",
            "LOGNAME",
            "PATH",
            "LANG",
            "LC_ALL",
            "SHELL",
            "TERM",
            "XDG_RUNTIME_DIR",
            "DBUS_SESSION_BUS_ADDRESS",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        }
        environment = {key: value for key, value in os.environ.items() if key in allowed}
        environment["PATH"] = f"{Path.home() / '.local/bin'}:/usr/local/bin:/usr/bin:/bin"
        environment["GIT_TERMINAL_PROMPT"] = "0"
        return environment

    def _proposal(self, proposal_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{32}", proposal_id):
            raise ValueError("invalid coding proposal id")
        proposal = self._proposals.get(proposal_id)
        if proposal is None:
            raise ValueError("coding proposal was not found")
        return proposal

    def _task_dir(self, proposal_id: str) -> Path:
        assert self.state_root is not None
        return self.state_root / proposal_id

    def _save(self, proposal: dict[str, Any]) -> None:
        if self.state_root is None:
            return
        task_dir = self._task_dir(str(proposal["proposal_id"]))
        task_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = task_dir / "task.json"
        temporary = task_dir / ".task.json.tmp"
        temporary.write_text(json.dumps(proposal, indent=2, sort_keys=True), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)

    def _load_existing(self) -> None:
        assert self.state_root is not None
        for path in self.state_root.glob("[0-9a-f]" * 32 + "/task.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                proposal_id = str(value["proposal_id"])
                if re.fullmatch(r"[0-9a-f]{32}", proposal_id):
                    self._proposals[proposal_id] = value
            except (OSError, json.JSONDecodeError, KeyError, TypeError):
                continue

    def _emit(self, event_type: str, payload: dict[str, Any], correlation_id: str) -> None:
        if self.event_sink is not None:
            self.event_sink(event_type, self._public(payload), correlation_id)

    @staticmethod
    def _public(proposal: dict[str, Any]) -> dict[str, Any]:
        result = json.loads(json.dumps(proposal))
        result.pop("diagnostics", None)
        if isinstance(result.get("codex"), dict):
            result["codex"].pop("stderr", None)
        return result

    @staticmethod
    def _task_summary(proposal: dict[str, Any]) -> dict[str, Any]:
        return {
            "proposal_id": proposal.get("proposal_id", ""),
            "request": str(proposal.get("request", ""))[:240],
            "project": proposal.get("project", ""),
            "status": proposal.get("status", "UNKNOWN"),
            "created_epoch": proposal.get("created_epoch"),
            "duration_ms": proposal.get("duration_ms"),
            "commit_id": proposal.get("commit_id", ""),
            "requires_security_review": bool(
                proposal.get("change_review", {}).get("requires_security_review", False)
            ),
        }

    @staticmethod
    def _redact_diagnostics(value: str) -> str:
        redacted = re.sub(
            r"(?i)\b(api[_-]?key|authorization|cookie|credential|password|secret|token)\b\s*[:=]\s*[^\s,;]+",
            r"\1=[REDACTED]",
            value,
        )
        return re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", redacted)

    @staticmethod
    def _run(
        command: list[str],
        cwd: Path | None = None,
        timeout: int = 10,
        environment: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
                env=environment,
            )
            return {
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        except (OSError, subprocess.TimeoutExpired) as error:
            return {"returncode": 124, "stdout": "", "stderr": str(error)}

    def _git(self, root: Path, *arguments: str, timeout: int) -> dict[str, Any]:
        return self._run(
            [_platform_executable("/usr/bin/git"), "-C", str(root), *arguments], timeout=timeout
        )
