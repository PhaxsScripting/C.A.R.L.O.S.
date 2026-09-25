"""Bounded condition monitoring. Waiting observes; it never repeats mutations."""

import asyncio
import json
import time

from .goals import validate_conditions, verify_conditions


async def wait_for_conditions(
    conditions, requester, correlation, *, timeout=60, interval=2, notify=None
):
    validate_conditions(conditions)
    if (
        len(conditions) > 3
        or type(timeout) not in (int, float)
        or not 1 <= timeout <= 1800
        or type(interval) not in (int, float)
        or not 0.5 <= interval <= 60
    ):
        raise ValueError("Wait requires 1–3 predicates, 1–1800 seconds, and 0.5–60 second polling")
    # At most 60 polls keeps CPU, event traffic and the task journal bounded.
    interval = max(interval, timeout / 59)
    started = time.monotonic()
    deadline = started + timeout
    previous, changes, polls = None, 0, 0
    last = {"verified": False, "conditions": []}
    if notify:
        notify("WAITING", {"timeout_seconds": timeout})
    try:
        while time.monotonic() < deadline:
            try:
                async with asyncio.timeout(max(0.001, deadline - time.monotonic())):
                    last = await verify_conditions(conditions, requester, correlation)
            except TimeoutError:
                break
            polls += 1
            signature = json.dumps(last.get("conditions", []), sort_keys=True)
            if previous is not None and signature != previous:
                changes += 1
                if notify:
                    notify("PROGRESS", {"polls": polls, "changes": changes})
            previous = signature
            if last.get("verified"):
                return {
                    **last,
                    "ok": True,
                    "wait_state": "SATISFIED",
                    "polls": polls,
                    "progress_changes": changes,
                    "wait_seconds": round(time.monotonic() - started, 3),
                    "goal_verified": False,
                }
            await asyncio.sleep(max(0, min(interval, deadline - time.monotonic())))
        return {
            **last,
            "ok": False,
            "verified": False,
            "wait_state": "TIMED_OUT",
            "polls": polls,
            "progress_changes": changes,
            "wait_seconds": round(time.monotonic() - started, 3),
            "error": "The observed completion conditions were not met before the wait deadline. No action was replayed.",
        }
    finally:
        if notify:
            notify(
                "WAIT_FINISHED",
                {"polls": polls, "elapsed_seconds": round(time.monotonic() - started, 3)},
            )
