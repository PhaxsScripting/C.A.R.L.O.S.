"""Explicitly saved, local personal items. No background collection or jobs."""

from __future__ import annotations

import time
import uuid
from typing import Any


class PersonalStore:
    KINDS = {"note", "task", "bookmark", "snippet"}

    def __init__(self, daily):
        self.daily = daily

    @staticmethod
    def initialize(db) -> None:
        db.execute("""CREATE TABLE IF NOT EXISTS personal_items (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
            content TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0,
            archived INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
            updated REAL NOT NULL, UNIQUE(kind, title COLLATE NOCASE))""")

    def _kind(self, kind: str) -> None:
        if kind not in self.KINDS:
            raise ValueError("Unknown personal item kind")

    def create(self, kind: str, title: str, content: str = "") -> dict[str, Any]:
        self._kind(kind)
        title = title.strip()
        if not title or len(title) > 160 or len(content) > 8000:
            raise ValueError("Use a 1-160 character title and at most 8000 characters of content")
        if kind == "bookmark":
            from .commands import normalize_web_url

            content = normalize_web_url(content)
        now = time.time()
        item = dict(
            id=uuid.uuid4().hex,
            kind=kind,
            title=title,
            content=content,
            done=0,
            archived=0,
            created=now,
            updated=now,
        )
        with self.daily.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM personal_items WHERE kind=? AND title=? COLLATE NOCASE",
                (kind, title),
            ).fetchone():
                raise ValueError(
                    f"That {kind} title already exists, possibly archived. Use another title or restore it."
                )
            db.execute(
                "INSERT INTO personal_items VALUES (:id,:kind,:title,:content,:done,:archived,:created,:updated)",
                item,
            )
        return {"verified": True, "item": item, "message": f"Saved {kind}: {title}."}

    def listing(self, kind: str, query: str = "", state: str = "active") -> dict[str, Any]:
        self._kind(kind)
        if state not in {"active", "done", "archived", "all"}:
            raise ValueError("Unknown item state")
        with self.daily.connect() as db:
            rows = db.execute(
                """SELECT * FROM personal_items WHERE kind=?
                AND (?='all' OR archived=?) AND (?!='done' OR done=1)
                AND (?!='task' OR ?!='active' OR done=0)
                AND (instr(lower(title),lower(?))>0 OR instr(lower(content),lower(?))>0)
                ORDER BY updated DESC, id LIMIT 101""",
                (kind, state, int(state == "archived"), state, kind, state, query, query),
            ).fetchall()
        items = [dict(row) for row in rows[:100]]
        # Listings show titles, not entire private note/snippet contents. Read is
        # a separate explicit request. Preserve immutable IDs for follow-ups.
        for item in items:
            item.pop("content")
        label = "tasks" if kind == "task" else kind + "s"
        message = (
            "; ".join(f"{i+1}. {item['title']}" for i, item in enumerate(items[:12]))
            or f"No matching {label}."
        )
        if len(items) > 12:
            message += f"; {len(items) - 12} more shown in the app."
        return {
            "items": items,
            "choice_kind": kind,
            "truncated": len(rows) > 100,
            "message": message,
        }

    @staticmethod
    def _one(db, kind: str, identifier: str):
        rows = db.execute(
            "SELECT * FROM personal_items WHERE kind=? AND (id=? OR title=? COLLATE NOCASE)",
            (kind, identifier, identifier),
        ).fetchall()
        if len(rows) != 1:
            raise ValueError(
                f"No unique {kind} with that exact title or ID. List your items first."
            )
        return dict(rows[0])

    def read(self, kind: str, identifier: str) -> dict[str, Any]:
        self._kind(kind)
        with self.daily.connect() as db:
            item = self._one(db, kind, identifier)
        return {
            "item": item,
            "message": item["title"] + (": " + item["content"][:2000] if item["content"] else "."),
        }

    def change(
        self, kind: str, identifier: str, operation: str, content: str = ""
    ) -> dict[str, Any]:
        self._kind(kind)
        if operation not in {"append", "complete", "reopen", "archive", "restore"}:
            raise ValueError("Unknown personal item action")
        if operation == "append" and (kind != "note" or not content.strip()):
            raise ValueError("Only notes accept nonempty appended text")
        if operation in {"complete", "reopen"} and kind != "task":
            raise ValueError("Only tasks have completion state")
        with self.daily.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            item = self._one(db, kind, identifier)
            if operation == "append":
                if item["archived"]:
                    raise ValueError("Restore that note before adding text")
                item["content"] += ("\n" if item["content"] else "") + content
                if len(item["content"]) > 8000:
                    raise ValueError("Note would exceed 8000 characters; nothing appended")
            elif operation in {"complete", "reopen"}:
                if item["archived"]:
                    raise ValueError("Restore that task first")
                item["done"] = int(operation == "complete")
            else:
                item["archived"] = int(operation == "archive")
            item["updated"] = time.time()
            db.execute(
                "UPDATE personal_items SET content=:content, done=:done, archived=:archived, updated=:updated WHERE id=:id",
                item,
            )
        verb = {
            "append": "Updated",
            "complete": "Completed",
            "reopen": "Reopened",
            "archive": "Archived",
            "restore": "Restored",
        }[operation]
        return {"verified": True, "item": item, "message": f"{verb} {kind}: {item['title']}."}

    def snooze(self, identifier: str, seconds: float) -> dict[str, Any]:
        if not 1 <= seconds <= 31622400:
            raise ValueError("Snooze must be between one second and one year")
        with self.daily.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT * FROM reminders WHERE state IN ('pending','fired') AND (id=? OR label=? COLLATE NOCASE)",
                (identifier, identifier),
            ).fetchall()
            if len(rows) != 1:
                raise ValueError("Specify one exact pending or recently fired reminder name or ID")
            item = dict(rows[0])
            due = time.time() + seconds
            db.execute("UPDATE reminders SET due=?, state='pending' WHERE id=?", (due, item["id"]))
        return {
            "verified": True,
            "message": f"Snoozed {item['label']} for {seconds:g} seconds.",
            "due": due,
        }
