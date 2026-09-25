from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    tags_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    correlation_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user','assistant','system','tool')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS conversations_correlation_idx ON conversations(correlation_id, id);
CREATE TABLE IF NOT EXISTS event_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    source TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS permission_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    correlation_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    permission TEXT NOT NULL,
    decision TEXT NOT NULL,
    arguments_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
INSERT OR IGNORE INTO metadata(key, value) VALUES ('schema_version', '1');
"""


def now() -> str:
    return datetime.now(UTC).isoformat()


class MemoryStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(SCHEMA)
        self._connection.commit()
        self._persistent_connection = self._connection
        self.private = False
        os.chmod(path, 0o600)

    def set_private(self, enabled: bool, *, guest: bool = False) -> None:
        """Use a disposable RAM database; exiting cannot copy private rows back."""
        with self._lock:
            if self.private:
                self._connection.close()
            self._connection = self._persistent_connection
            self.private = enabled
            if enabled:
                self._connection = sqlite3.connect(":memory:", check_same_thread=False)
                self._connection.row_factory = sqlite3.Row
                if guest:
                    self._connection.executescript(SCHEMA)
                else:
                    self._persistent_connection.backup(self._connection)

    def close(self) -> None:
        with self._lock:
            self._connection.close()
            if self.private:
                self._persistent_connection.close()

    def remember(self, content: str, tags: list[str] | None = None) -> dict[str, Any]:
        memory_id = uuid.uuid4().hex
        timestamp = now()
        clean_tags = sorted({tag.strip().lower() for tag in (tags or []) if tag.strip()})[:20]
        with self._lock:
            self._connection.execute(
                "INSERT INTO memories(id, content, tags_json, created_at, updated_at) VALUES(?,?,?,?,?)",
                (memory_id, content.strip(), json.dumps(clean_tags), timestamp, timestamp),
            )
            self._connection.commit()
        return {
            "id": memory_id,
            "content": content.strip(),
            "tags": clean_tags,
            "created_at": timestamp,
        }

    def list_memories(self, query: str = "", limit: int = 50) -> list[dict[str, Any]]:
        bounded = max(1, min(limit, 200))
        with self._lock:
            if query:
                rows = self._connection.execute(
                    "SELECT * FROM memories WHERE content LIKE ? ORDER BY updated_at DESC LIMIT ?",
                    (f"%{query}%", bounded),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT * FROM memories ORDER BY updated_at DESC LIMIT ?", (bounded,)
                ).fetchall()
        return [
            {
                "id": row["id"],
                "content": row["content"],
                "tags": json.loads(row["tags_json"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def forget(self, memory_id: str) -> bool:
        with self._lock:
            cursor = self._connection.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            self._connection.commit()
            return cursor.rowcount == 1

    def add_conversation(self, correlation_id: str, role: str, content: str) -> None:
        if role not in {"user", "assistant", "system", "tool"}:
            raise ValueError("invalid conversation role")
        with self._lock:
            self._connection.execute(
                "INSERT INTO conversations(correlation_id, role, content, created_at) VALUES(?,?,?,?)",
                (correlation_id, role, content[:32_000], now()),
            )
            self._connection.commit()

    def recent_conversation(self, limit: int = 40) -> list[dict[str, Any]]:
        bounded = max(1, min(limit, 200))
        with self._lock:
            rows = self._connection.execute(
                "SELECT correlation_id, role, content, created_at FROM conversations ORDER BY id DESC LIMIT ?",
                (bounded,),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def record_event(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO event_summaries(sequence,event_type,source,correlation_id,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                (
                    int(event["sequence"]),
                    str(event["type"]),
                    str(event["source"]),
                    str(event["correlation_id"]),
                    json.dumps(event.get("payload", {}), separators=(",", ":")),
                    str(event["timestamp"]),
                ),
            )
            self._connection.commit()

    def record_permission(
        self,
        correlation_id: str,
        tool_name: str,
        permission: str,
        decision: str,
        arguments_hash: str,
    ) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO permission_audit(correlation_id,tool_name,permission,decision,arguments_hash,created_at) VALUES(?,?,?,?,?,?)",
                (correlation_id, tool_name, permission, decision, arguments_hash, now()),
            )
            self._connection.commit()

    def stats(self) -> dict[str, int]:
        with self._lock:
            memories = self._connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            conversations = self._connection.execute(
                "SELECT COUNT(*) FROM conversations"
            ).fetchone()[0]
            events = self._connection.execute("SELECT COUNT(*) FROM event_summaries").fetchone()[0]
        return {"memories": memories, "conversations": conversations, "events": events}
