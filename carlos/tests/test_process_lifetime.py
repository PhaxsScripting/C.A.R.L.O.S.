import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock

from ev.goals import validate_conditions, verify_conditions
from ev.task_wait import wait_for_conditions
from ev.tools.base import ValidationError
from ev.tools.process_lifetime import process_lifetime


class ProcessLifetimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_owned_child_waits_for_exact_lifetime_without_claiming_exit_success(self):
        child = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(.7)"
        )
        try:
            identity = process_lifetime({"pid": child.pid}, None)
            self.assertEqual(identity["lifetime_status"], "RUNNING")
            condition = {
                "kind": "process_ended",
                **{k: identity[k] for k in ("pid", "start_ticks", "boot_id")},
            }

            async def request(payload, correlation):
                return {
                    "status": "completed",
                    "result": process_lifetime(payload["arguments"], None),
                }

            result = await wait_for_conditions(
                [condition], request, "fixture", timeout=3, interval=0.5
            )
            self.assertTrue(result["verified"])
            self.assertFalse(result["goal_verified"])
            after = process_lifetime(
                {"pid": child.pid, "start_ticks": identity["start_ticks"]}, None
            )
            self.assertFalse(after["exit_code_known"])
            self.assertFalse(after["task_success_verified"])
        finally:
            await child.wait()

    async def test_missing_identity_reboot_or_running_process_does_not_verify(self):
        identity = process_lifetime({"pid": os.getpid()}, None)
        condition = {
            "kind": "process_ended",
            **{k: identity[k] for k in ("pid", "start_ticks", "boot_id")},
        }
        request = AsyncMock(return_value={"status": "completed", "result": identity})
        self.assertFalse((await verify_conditions([condition], request, "fixture"))["verified"])
        for data in (
            {},
            {**identity, "boot_id": "another boot", "lifetime_status": "ABSENT"},
            {**identity, "pid": os.getpid() + 1, "lifetime_status": "ABSENT"},
        ):
            request.return_value = {"status": "completed", "result": data}
            self.assertFalse((await verify_conditions([condition], request, "fixture"))["verified"])
        with self.assertRaises(ValidationError):
            validate_conditions([{"kind": "process_ended", "pid": os.getpid()}])

    async def test_pid_reuse_is_detected_without_signalling_new_process(self):
        identity = process_lifetime({"pid": os.getpid()}, None)
        result = process_lifetime(
            {"pid": os.getpid(), "start_ticks": str(int(identity["start_ticks"]) + 1)}, None
        )
        self.assertEqual(result["lifetime_status"], "REPLACED")
        self.assertFalse(result["task_success_verified"])
