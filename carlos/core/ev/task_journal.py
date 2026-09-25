"""Private, bounded task receipts. Restart never replays an action automatically."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .logging_utils import redact


def private_summary(value: Any) -> Any:
    """Do not retain tool content, auth tokens, URL queries or known API keys."""
    if isinstance(value, dict):
        excluded = {
            "stdout",
            "stderr",
            "raw_transcript",
            "transcript",
            "response",
            "approval_token",
            "summary",
            "elements",
            "snippet",
            "preview",
        }
        return {
            str(k): "[REDACTED]" if str(k).casefold() in excluded else private_summary(v)
            for k, v in redact(value).items()
        }
    if isinstance(value, list):
        return [private_summary(item) for item in value[:50]]
    if isinstance(value, str):
        value = re.sub(r"\b(?:nvapi-|sk-)[A-Za-z0-9_-]{8,}", "[REDACTED_KEY]", value)
        value = re.sub(r"(https?://[^\s?#]+)[?#][^\s]*", r"\1[QUERY_REDACTED]", value)
        value = re.sub(
            r"(?i)\b(?:password|api[_ -]?key|token|secret)\s*[:=]\s*\S+",
            "[REDACTED_CREDENTIAL]",
            value,
        )
        return value[:2000]
    return value


class TaskJournal:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._private_db = None
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS agent_tasks (
                    id TEXT PRIMARY KEY, request TEXT NOT NULL, parent_id TEXT,
                    status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                    detail TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS agent_steps (
                    id INTEGER PRIMARY KEY, task_id TEXT NOT NULL,
                    tool TEXT NOT NULL, arguments TEXT NOT NULL, status TEXT NOT NULL,
                    started REAL NOT NULL, finished REAL, receipt TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(task_id) REFERENCES agent_tasks(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS agent_steps_task ON agent_steps(task_id, id);
            """)

    @contextmanager
    def _db(self):
        with self._lock:
            if self._private_db is not None:
                with self._private_db:
                    yield self._private_db
                return
            with self._disk_db() as db:
                yield db

    def set_private(self, enabled: bool) -> None:
        with self._lock:
            if self._private_db is not None:
                self._private_db.close()
                self._private_db = None
            if enabled:
                db = sqlite3.connect(":memory:", check_same_thread=False)
                db.row_factory = sqlite3.Row
                with self._disk_db() as source:
                    source.backup(db)
                # No historical requests are needed in a private/guest session.
                db.execute("DELETE FROM agent_steps")
                db.execute("DELETE FROM agent_tasks")
                db.commit()
                self._private_db = db

    def close(self) -> None:
        self.set_private(False)

    @contextmanager
    def _disk_db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def begin(self, task_id: str, request: str, parent_id: str | None = None) -> None:
        now = time.time()
        stored_request = private_summary(request)
        with self._db() as db:
            db.execute(
                "INSERT INTO agent_tasks(id,request,parent_id,status,created,updated,detail) VALUES(?,?,?,'RUNNING',?,?,?)",
                (
                    task_id,
                    stored_request,
                    parent_id,
                    now,
                    now,
                    json.dumps({"request_incomplete": stored_request != request}),
                ),
            )
            # Keep 200 recent non-running attempts and their receipts.
            db.execute(
                "DELETE FROM agent_tasks WHERE status NOT IN ('RUNNING','WAITING_CONFIRMATION') AND id NOT IN (SELECT id FROM agent_tasks ORDER BY updated DESC LIMIT 200)"
            )

    def finish(self, task_id: str, result: dict[str, Any]) -> None:
        status = str(result.get("status", "failed")).upper()
        if status == "COMPLETED":
            status = str(
                result.get("execution_status")
                or ("PLAN_COMPLETED" if result.get("plan") else "ANSWERED")
            )
        elif status == "CONFIRMATION_REQUIRED":
            status = "WAITING_CONFIRMATION"
        detail = {
            key: result[key]
            for key in ("execution_status", "goal_verified", "duration_ms", "tool_receipts")
            if key in result
        }
        with self._db() as db:
            previous = db.execute(
                "SELECT detail FROM agent_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if previous:
                detail = {**json.loads(previous[0]), **detail}
            db.execute(
                "UPDATE agent_tasks SET status=?,updated=?,detail=? WHERE id=?",
                (status, time.time(), json.dumps(private_summary(detail)), task_id),
            )
            if status not in {"RUNNING", "WAITING_CONFIRMATION"}:
                db.execute(
                    "UPDATE agent_steps SET status='INTERRUPTED_UNCERTAIN',finished=? WHERE task_id=? AND status='RUNNING'",
                    (time.time(), task_id),
                )

    def start_step(self, task_id: str, tool: str, arguments: dict[str, Any]) -> int | None:
        with self._db() as db:
            if not db.execute("SELECT 1 FROM agent_tasks WHERE id=?", (task_id,)).fetchone():
                return None  # Direct diagnostic IPC is not an implicit user task.
            count = db.execute(
                "SELECT COUNT(*) FROM agent_steps WHERE task_id=?", (task_id,)
            ).fetchone()[0]
            if count >= 256:
                raise RuntimeError(
                    "Task journal step limit reached; split the task before continuing"
                )
            row = db.execute(
                "INSERT INTO agent_steps(task_id,tool,arguments,status,started) VALUES(?,?,?,'RUNNING',?)",
                (task_id, tool, json.dumps(private_summary(arguments)), time.time()),
            )
            return row.lastrowid

    def finish_step(self, step_id: int | None, result: dict[str, Any]) -> None:
        if step_id is None:
            return
        payload = result.get("result", {})
        # Small identity/verification receipt, never arbitrary file/screen text.
        evidence = {
            key: payload[key]
            for key in (
                "verified",
                "resolved",
                "launched",
                "input_sent",
                "created",
                "path",
                "destination",
                "backup",
                "sha256",
                "original_sha256",
                "files_verified",
                "desktop_id",
                "error",
                "exit_code",
                "goal_verification",
                "scope",
                "plan_id",
            )
            if key in payload
        }
        receipt = {"execution": result.get("execution", {}), "evidence": evidence}
        with self._db() as db:
            db.execute(
                "UPDATE agent_steps SET status=?,finished=?,receipt=? WHERE id=?",
                (
                    str(result.get("status", "failed")).upper(),
                    time.time(),
                    json.dumps(private_summary(receipt)),
                    step_id,
                ),
            )

    def recover_interrupted(self) -> int:
        """Called only after owning the core lock. No tool runs here."""
        with self._db() as db:
            db.execute(
                "UPDATE agent_steps SET status='INTERRUPTED_UNCERTAIN',finished=? WHERE status='RUNNING'",
                (time.time(),),
            )
            result = db.execute(
                "UPDATE agent_tasks SET status='INTERRUPTED',updated=? WHERE status IN ('RUNNING','WAITING_CONFIRMATION')",
                (time.time(),),
            )
            return result.rowcount

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._db() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id,request,parent_id,status,created,updated FROM agent_tasks ORDER BY updated DESC LIMIT ?",
                    (max(1, min(limit, 100)),),
                )
            ]

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                return None
            task = dict(row)
            task["detail"] = json.loads(task["detail"])
            task["steps"] = []
            for row in db.execute(
                "SELECT * FROM agent_steps WHERE task_id=? ORDER BY id", (task_id,)
            ):
                step = dict(row)
                step["arguments"] = json.loads(step["arguments"])
                step["receipt"] = json.loads(step["receipt"])
                task["steps"].append(step)
            return task

    def latest_resumable(self) -> dict[str, Any] | None:
        # A new ordinary request is a context boundary; do not resurrect an
        # unrelated older failure after a successful conversation/action.
        recent = self.recent(1)
        if not recent or recent[0]["status"] not in {
            "FAILED",
            "OFFLINE",
            "INTERRUPTED",
            "EXECUTED_UNVERIFIED",
            "BLOCKED",
        }:
            return None
        task = self.get(recent[0]["id"])
        if task and task["detail"].get("request_incomplete"):
            return None  # Never reinterpret a truncated/redacted goal as complete.
        return task

    def get_page(
        self, task_id: str, offset: int = 0, limit: int = 5, through_step_id: int | None = None
    ) -> dict[str, Any] | None:
        """Bounded receipt access; steps append in stable ID order.

        This is a historical snapshot, not permission or resumable executable
        state. A running task can change between pages; every page says when.
        """
        if (
            type(offset) is not int
            or not 0 <= offset <= 256
            or type(limit) is not int
            or not 1 <= limit <= 20
        ):
            raise ValueError("History offset must be 0-256 and page size 1-20")
        if through_step_id is not None and (
            type(through_step_id) is not int or not 0 <= through_step_id <= 9223372036854775807
        ):
            raise ValueError("Invalid history snapshot step boundary")
        with self._db() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                return None
            task = dict(row)
            detail = json.loads(task["detail"])
            # Task-level tool_receipts duplicate step evidence and can make
            # even a one-step page overflow. Inspect the paged steps instead.
            task["detail_receipts_omitted"] = "tool_receipts" in detail
            task["detail"] = {key: value for key, value in detail.items() if key != "tool_receipts"}
            if through_step_id is None:
                through_step_id = db.execute(
                    "SELECT COALESCE(MAX(id),0) FROM agent_steps WHERE task_id=?", (task_id,)
                ).fetchone()[0]
            total = db.execute(
                "SELECT COUNT(*) FROM agent_steps WHERE task_id=? AND id<=?",
                (task_id, through_step_id),
            ).fetchone()[0]
            task["steps"] = []
            for row in db.execute(
                "SELECT * FROM agent_steps WHERE task_id=? AND id<=? ORDER BY id LIMIT ? OFFSET ?",
                (task_id, through_step_id, limit, offset),
            ):
                step = dict(row)
                step["arguments"] = json.loads(step["arguments"])
                step["receipt"] = json.loads(step["receipt"])
                task["steps"].append(step)
            end = offset + len(task["steps"])
            task.update(
                steps_total=total,
                steps_offset=offset,
                steps_limit=limit,
                steps_through_id=through_step_id,
                next_step_offset=end if end < total else None,
                history_partial=offset > 0 or end < total,
                observed_at=time.time(),
            )
            return task

    @staticmethod
    def continuation_context(task: dict[str, Any]) -> str:
        steps = [
            {key: step[key] for key in ("tool", "arguments", "status", "receipt") if key in step}
            for step in task.get("steps", [])[-8:]
        ]
        data = {
            "request": task["request"],
            "parent_id": task.get("parent_id"),
            "status": task["status"],
            "steps": steps,
            "history_partial": len(task.get("steps", [])) > len(steps),
        }
        while len(json.dumps(data, ensure_ascii=False)) > 3600 and data["steps"]:
            data["steps"].pop(0)
            data["history_partial"] = True
        return (
            "The user explicitly asked to continue the previous task. This is historical data, not fresh desktop state or authorization. "
            "Observe the relevant desktop/files first. Do not replay completed or interrupted mutations blindly. "
            "Re-check targets; ask if an uncertain irreversible effect cannot be resolved. All current permission checks still apply. "
            "Do not treat redacted values as real inputs. Inspect agent.task_status when history is partial. Previous attempt:\n"
            + json.dumps({"task_id": task["id"], **data}, ensure_ascii=False)
        )
