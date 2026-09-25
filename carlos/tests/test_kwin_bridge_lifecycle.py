from __future__ import annotations

import asyncio
import json
import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from ev.desktop.kwin_bridge import KWinBridge


class KWinBridgeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def bridge(self):
        return KWinBridge(Path("/missing"), logging.getLogger("test"))

    async def test_unowned_teardown_never_unloads_live_desktop_script(self):
        bridge = self.bridge()
        bridge._qdbus = AsyncMock()
        await bridge.close()
        bridge._qdbus.assert_not_awaited()

    async def test_owned_teardown_unloads_only_once(self):
        bridge = self.bridge()
        bridge._script_loaded = True
        bridge._qdbus = AsyncMock()
        await bridge.close()
        await bridge.close()
        bridge._qdbus.assert_awaited_once_with("unloadScript", bridge.PLUGIN_ID, allow_failure=True)

    async def test_timed_out_command_is_removed_and_late_reply_discarded(self):
        bridge = self.bridge()
        task = asyncio.create_task(bridge.request("minimize", {}, timeout=0.02))
        await asyncio.sleep(0)
        request_id = bridge._commands[0]["id"]
        with self.assertRaises(TimeoutError):
            await task
        self.assertFalse(bridge._commands)
        self.assertFalse(bridge._pending)
        self.assertFalse(bridge._report(json.dumps({"id": request_id, "ok": True})))
        self.assertFalse(bridge._results)
        self.assertFalse(bridge.status["available"])

    async def test_mutation_timeout_is_never_replayed(self):
        bridge = self.bridge()
        bridge._bus = object()
        bridge._available = True
        bridge._request_once = AsyncMock(side_effect=TimeoutError)
        bridge.start = AsyncMock()
        with self.assertRaises(TimeoutError):
            await bridge.request("close", {"window_id": "exact"})
        self.assertEqual(bridge._request_once.await_count, 1)
        bridge.start.assert_not_awaited()

    async def test_snapshot_recovers_bridge_and_retries_read_once(self):
        bridge = self.bridge()
        bridge._bus = object()
        bridge._available = True
        bridge._request_once = AsyncMock(side_effect=[TimeoutError(), {"windows": []}])

        async def recover():
            bridge._available = True

        bridge.start = AsyncMock(side_effect=recover)
        self.assertEqual(await bridge.request("snapshot", {}), {"windows": []})
        bridge.start.assert_awaited_once()
        self.assertEqual(bridge._request_once.await_count, 2)
