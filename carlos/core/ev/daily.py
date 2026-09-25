from __future__ import annotations

from ev.platform import executable as _platform_executable

import asyncio
import json
import sqlite3
import time
import uuid
import threading
from contextlib import contextmanager, closing
from pathlib import Path
from typing import Any

from .events import PhaxEventBus
from .personal import PersonalStore


class DailyStore:
    """Durable user-created reminders, aliases and routines; never shell jobs."""

    def __init__(self, path: Path):
        self.path = path
        self._private_db = None
        self._privacy_lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS reminders (
                    id TEXT PRIMARY KEY, label TEXT NOT NULL, due REAL NOT NULL,
                    repeat_seconds REAL NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'pending',
                    created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS records (
                    kind TEXT NOT NULL, name TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(kind, name)
                );
            """)
            PersonalStore.initialize(db)
        path.chmod(0o600)
        self.personal = PersonalStore(self)

    @contextmanager
    def connect(self):
        with self._privacy_lock:
            db = self._private_db or sqlite3.connect(self.path, timeout=5)
            db.row_factory = sqlite3.Row
            try:
                with db:
                    yield db
            finally:
                if db is not self._private_db:
                    db.close()

    def set_private(self, enabled, *, guest=False):
        with self._privacy_lock:
            if self._private_db is not None:
                self._private_db.close()
                self._private_db = None
            if enabled:
                db = sqlite3.connect(":memory:", check_same_thread=False)
                with closing(sqlite3.connect(self.path)) as disk:
                    if guest:
                        for row in disk.execute(
                            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type='index'"
                        ):
                            db.execute(row[0])
                    else:
                        disk.backup(db)
                db.row_factory = sqlite3.Row
                self._private_db = db

    def close(self):
        """Discard private RAM state without persisting it."""
        self.set_private(False)

    def reminders(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM reminders WHERE state='pending' ORDER BY due LIMIT 200"
                )
            ]

    def add_reminder(self, label: str, seconds: float, repeat_seconds: float = 0) -> dict[str, Any]:
        if (
            not 1 <= seconds <= 366 * 86400
            or not 0 <= repeat_seconds <= 366 * 86400
            or (repeat_seconds and repeat_seconds < 60)
        ):
            raise ValueError(
                "Delay must be 1 second to a year; repeats must be at least one minute"
            )
        if not label.strip() or len(label) > 500:
            raise ValueError("Reminder label must be 1-500 characters")
        reminder = dict(
            id=uuid.uuid4().hex,
            label=label.strip(),
            due=time.time() + seconds,
            repeat_seconds=repeat_seconds,
            state="pending",
            created=time.time(),
        )
        with self.connect() as db:
            db.execute(
                "INSERT INTO reminders VALUES (:id,:label,:due,:repeat_seconds,:state,:created)",
                reminder,
            )
        return {"verified": True, "reminder": reminder, "message": f"Reminder set: {label}."}

    def cancel_reminder(self, identifier: str) -> dict[str, Any]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT id FROM reminders WHERE state='pending' AND (id=? OR lower(label)=lower(?))",
                (identifier, identifier),
            ).fetchall()
            if len(rows) != 1:
                return {
                    "verified": False,
                    "message": "Specify one exact reminder name or ID.",
                    "matches": len(rows),
                }
            db.execute("UPDATE reminders SET state='cancelled' WHERE id=?", (rows[0][0],))
        return {"verified": True, "message": "Reminder cancelled."}

    def due(self, now: float) -> list[dict[str, Any]]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM reminders WHERE state='pending' AND due<=? ORDER BY due LIMIT 20",
                    (now,),
                )
            ]
            for row in rows:
                repeat = row["repeat_seconds"]
                if repeat:
                    next_due = row["due"] + (int((now - row["due"]) // repeat) + 1) * repeat
                    db.execute("UPDATE reminders SET due=? WHERE id=?", (next_due, row["id"]))
                else:
                    db.execute("UPDATE reminders SET state='fired' WHERE id=?", (row["id"],))
        return rows

    def records(self, kind: str) -> dict[str, Any]:
        with self.connect() as db:
            return {
                row["name"]: json.loads(row["payload"])
                for row in db.execute(
                    "SELECT name,payload FROM records WHERE kind=? ORDER BY name", (kind,)
                )
            }

    def save(self, kind: str, name: str, payload: Any) -> dict[str, Any]:
        name = name.strip().casefold()
        if not name or len(name) > 100:
            raise ValueError("Name must be 1-100 characters")
        with self.connect() as db:
            db.execute(
                "INSERT INTO records VALUES (?,?,?) ON CONFLICT(kind,name) DO UPDATE SET payload=excluded.payload",
                (kind, name, json.dumps(payload)),
            )
        return {"verified": True, "name": name, "message": f"Saved {kind}: {name}."}

    def remove(self, kind: str, name: str) -> dict[str, Any]:
        with self.connect() as db:
            changed = db.execute(
                "DELETE FROM records WHERE kind=? AND name=?", (kind, name.casefold().strip())
            ).rowcount
        return {
            "verified": bool(changed),
            "message": f"Removed {kind}: {name}." if changed else "No matching saved item.",
        }


class PowerController:
    COMMANDS = {
        "shutdown": [
            _platform_executable("/usr/bin/qdbus6"),
            "org.kde.Shutdown",
            "/Shutdown",
            "org.kde.Shutdown.logoutAndShutdown",
        ],
        "reboot": [
            _platform_executable("/usr/bin/qdbus6"),
            "org.kde.Shutdown",
            "/Shutdown",
            "org.kde.Shutdown.logoutAndReboot",
        ],
        "logout": [
            _platform_executable("/usr/bin/qdbus6"),
            "org.kde.Shutdown",
            "/Shutdown",
            "org.kde.Shutdown.logout",
        ],
        "lock": [
            _platform_executable("/usr/bin/qdbus6"),
            "org.freedesktop.ScreenSaver",
            "/ScreenSaver",
            "org.freedesktop.ScreenSaver.Lock",
        ],
        "suspend": [
            _platform_executable("/usr/bin/qdbus6"),
            "--system",
            "org.freedesktop.login1",
            "/org/freedesktop/login1",
            "org.freedesktop.login1.Manager.Suspend",
            "false",
        ],
    }

    def __init__(self, bus: PhaxEventBus):
        self.bus = bus
        self.task: asyncio.Task | None = None
        self.pending: dict[str, Any] = {}

    async def request(self, action: str) -> dict[str, Any]:
        if action not in self.COMMANDS:
            raise ValueError("Unsupported power action")
        if self.task and not self.task.done():
            return {
                "verified": False,
                "message": "A power action is already pending; cancel it first.",
            }
        delay = 10 if action in {"shutdown", "reboot", "logout"} else 2
        self.pending = {"action": action, "due": time.time() + delay, "phase": "countdown"}
        self.task = asyncio.create_task(self._dispatch(action, delay))
        self.bus.publish("power.scheduled", "system", dict(self.pending))
        return {
            "verified": True,
            "scheduled": True,
            **self.pending,
            "message": f"{action.capitalize()} scheduled in {delay} seconds. Say cancel shutdown to cancel. Unsaved-work dialogs may still need attention.",
        }

    async def _dispatch(self, action: str, delay: float) -> None:
        process = None
        try:
            await asyncio.sleep(delay)
            self.pending["phase"] = "dispatching"
            self.bus.publish("power.dispatching", "system", {"action": action})
            process = await asyncio.create_subprocess_exec(
                *self.COMMANDS[action],
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _stdout, stderr = await asyncio.wait_for(process.communicate(), 8)
            if process.returncode:
                raise RuntimeError(stderr.decode(errors="replace")[:300])
            self.bus.publish(
                "power.requested",
                "system",
                {"action": action, "accepted": True, "completion_verified": False},
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.bus.publish("system.error", "system", {"message": f"{action} failed: {error}"})
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            self.pending = {}

    async def cancel(self) -> dict[str, Any]:
        if self.pending.get("phase") == "dispatching":
            return {
                "verified": False,
                "cancelled": False,
                "message": "The power request has already been sent to the OS; E.V. cannot promise to cancel it.",
            }
        task, self.task = self.task, None
        active = bool(task and not task.done())
        if active:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.pending = {}
        self.bus.publish("power.cancelled", "system", {"cancelled": active})
        return {
            "verified": True,
            "cancelled": active,
            "message": (
                "Pending power action cancelled." if active else "No power action is pending."
            ),
        }
