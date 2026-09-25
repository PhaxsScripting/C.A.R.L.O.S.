"""Durable read-only watches of explicitly observed same-user process lifetimes.

No commands, signals, stored code, continuation actions or success inference.
Restart resumes observation, not execution. Reboot and clock rollback fail closed.
"""

import math
import threading
import time
import uuid

from .goals import validate_conditions
from .tools.process_lifetime import process_lifetime


class ProcessWatches:
    def __init__(self, store, *, observe=process_lifetime, clock=time.time):
        self.store, self.observe, self.clock = store, observe, clock
        self.lock = threading.RLock()

    def list(self):
        with self.lock:
            return list(self.store.records("process_watch").values())

    def create(self, identity, timeout=3600):
        validate_conditions([{"kind": "process_ended", **identity}])
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or not 1 <= timeout <= 86400
        ):
            raise ValueError("Watch timeout must be 1 second to 24 hours")
        with self.lock:
            records = self.store.records("process_watch")
            current = self.observe(
                {"pid": identity["pid"], "start_ticks": identity["start_ticks"]}, None
            )
            if (
                any(current.get(k) != v for k, v in identity.items())
                or current.get("lifetime_status") != "RUNNING"
            ):
                raise ValueError(
                    "The exact observed process is no longer running; no watch created"
                )
            for existing in records.values():
                if (
                    isinstance(existing, dict)
                    and existing.get("status") == "WAITING"
                    and existing.get("identity") == identity
                ):
                    if self.clock() >= existing["deadline"]:
                        raise ValueError(
                            "The existing watch deadline has expired; inspect its reconciled status before registering another"
                        )
                    return existing  # Idempotent registration, never extends a deadline.
            if (
                sum(isinstance(r, dict) and r.get("status") == "WAITING" for r in records.values())
                >= 8
            ):
                raise ValueError("Eight background watches are already active")
            now = self.clock()
            record = {
                "id": uuid.uuid4().hex,
                "identity": dict(identity),
                "status": "WAITING",
                "created": now,
                "deadline": now + timeout,
                "updated": now,
                "task_success_verified": False,
                "actions_replayed": False,
                "exit_code_known": False,
            }
            terminal = [
                (key, value)
                for key, value in records.items()
                if isinstance(value, dict) and value.get("status") != "WAITING"
            ]
            for key, _ in sorted(terminal, key=lambda item: item[1].get("updated", 0))[:-23]:
                self.store.remove("process_watch", key)
            self.store.save("process_watch", record["id"], record)
            if self.store.records("process_watch").get(record["id"]) != record:
                raise RuntimeError("Process watch persistence could not be verified")
            return record

    def cancel(self, identifier=None):
        with self.lock:
            records = self.store.records("process_watch")
            if identifier is not None and identifier not in records:
                raise ValueError("Unknown exact process-watch ID")
            cancelled = []
            for key, record in records.items():
                if (
                    isinstance(record, dict)
                    and (identifier is None or key == identifier)
                    and record.get("status") == "WAITING"
                ):
                    record.update(status="CANCELLED", updated=self.clock())
                    self.store.save("process_watch", key, record)
                    cancelled.append(key)
            return {"cancelled_watch_ids": cancelled, "processes_signalled": 0}

    def tick(self):
        changes = []
        with self.lock:
            for identifier, record in self.store.records("process_watch").items():
                if not isinstance(record, dict) or record.get("status") != "WAITING":
                    continue
                now = self.clock()
                try:
                    identity = record["identity"]
                    validate_conditions([{"kind": "process_ended", **identity}])
                    created, deadline = record["created"], record["deadline"]
                    if (
                        any(
                            type(v) not in (int, float) or not math.isfinite(v)
                            for v in (created, deadline)
                        )
                        or not 1 <= deadline - created <= 86400
                    ):
                        raise ValueError("Invalid stored watch deadline")
                    if now < created:
                        status = "CLOCK_CHANGED"
                    elif now >= deadline:
                        status = "TIMED_OUT"
                    else:
                        current = self.observe(
                            {"pid": identity["pid"], "start_ticks": identity["start_ticks"]}, None
                        )
                        if (
                            current.get("boot_id") != identity["boot_id"]
                            or current.get("pid") != identity["pid"]
                        ):
                            status = "IDENTITY_CHANGED"
                        elif current.get("lifetime_status") in {"ABSENT", "EXITED", "REPLACED"}:
                            status = "LIFETIME_ENDED"
                        elif (
                            current.get("lifetime_status") == "RUNNING"
                            and current.get("start_ticks") == identity["start_ticks"]
                        ):
                            continue
                        else:
                            status = "OBSERVATION_FAILED"
                except (ValueError, TypeError, KeyError, OSError):
                    status = "OBSERVATION_FAILED"
                record.update(
                    status=status,
                    updated=now,
                    task_success_verified=False,
                    actions_replayed=False,
                    exit_code_known=False,
                )
                self.store.save("process_watch", identifier, record)
                changes.append(record)
        return changes
