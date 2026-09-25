import tempfile, unittest, json
from pathlib import Path
from unittest.mock import AsyncMock
from ev.events import PhaxEventBus
from ev.identity import identity
from ev.paths import Paths
from ev.service import CarlosCore


class CarlosTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        p = Path(self.tmp.name)
        self.core = CarlosCore(
            paths=Paths(*(p / x for x in ("config", "data", "state", "cache", "runtime")))
        )

    async def asyncTearDown(self):
        self.core.daily.close()
        self.core.task_journal.close()
        self.core.memory.close()
        self.tmp.cleanup()

    async def test_identity_preserves_protocol_and_storage(self):
        r = await self.core.handle_request({"type": "carlos.status", "payload": {}})
        self.assertEqual(r["name"], "Carlos")
        self.assertEqual(r["expansion"], "Crackhead Artificial Robot Living On Shitbox")
        self.assertEqual(self.core.paths.socket.name, "ev.sock")
        event = self.core.bus.publish("core.state_changed", "core", {"to": "USING_TOOL"}).as_dict()
        self.assertEqual(event["type"], "core.state_changed")
        self.assertEqual(event["name"], "carlos.executing")
        self.assertEqual(event["protocol_version"], 1)

    async def test_destructive_denial_preserves_data_and_token_cannot_replay(self):
        record = self.core.memory.remember("test record")
        r = await self.core.request_tool(
            {"name": "memory.forget", "arguments": {"id": record["id"]}}, "test"
        )
        self.assertEqual(r["status"], "confirmation_required")
        p = r["confirmation"]
        answer = {"id": p["id"], "approval_token": p["approval_token"], "approved": False}
        r = await self.core.resolve_confirmation(answer)
        self.assertEqual(r["status"], "denied")
        self.assertEqual(len(self.core.memory.list_memories()), 1)
        with self.assertRaises(ValueError):
            await self.core.resolve_confirmation(answer)

    async def test_support_does_not_include_private_content(self):
        self.core.memory.remember("SENSITIVE_TEST_CONTENT")
        r = await self.core.handle_request({"type": "carlos.support", "payload": {}})
        self.assertNotIn("SENSITIVE_TEST_CONTENT", json.dumps(r))
        self.assertNotIn("provider", r)

    async def test_private_memory_and_receipts_never_reach_disk(self):
        from ev.memory import MemoryStore
        from ev.task_journal import TaskJournal

        self.core._stop_all_actions = AsyncMock(return_value={})
        self.core.voice.set_privacy_mode = AsyncMock(return_value={})
        self.core.memory.remember("existing persistent memory")
        await self.core.privacy.set_mode("PRIVATE SESSION")
        self.core.memory.add_conversation("private", "user", "PRIVATE_CANARY")
        self.core.task_journal.begin("private", "PRIVATE_CANARY")
        self.assertTrue(self.core.bus.publish("test", "test").private)
        disk = MemoryStore(self.core.paths.database)
        self.assertNotIn("PRIVATE_CANARY", json.dumps(disk.recent_conversation()))
        disk.close()
        disk_journal = TaskJournal(self.core.task_journal.path)
        self.assertIsNone(disk_journal.get("private"))
        await self.core.privacy.set_mode("NORMAL")
        self.assertNotIn("PRIVATE_CANARY", json.dumps(self.core.memory.recent_conversation()))
        self.assertEqual(len(self.core.memory.list_memories()), 1)
        self.assertIsNone(self.core.task_journal.get("private"))

    async def test_guest_hides_context_and_denies_personal_tools(self):
        self.core._stop_all_actions = AsyncMock(return_value={})
        self.core.voice.set_privacy_mode = AsyncMock(return_value={})
        self.core.memory.remember("PERSONAL_CANARY")
        await self.core.privacy.set_mode("GUEST")
        self.assertEqual(self.core.memory.list_memories(), [])
        self.assertNotIn("PERSONAL_CANARY", str(await self.core._model_context("personal")))
        r = await self.core.handle_request({"type": "memory.list", "payload": {}})
        self.assertEqual(r["status"], "denied")
        r = await self.core.request_tool(
            {"name": "memory.remember", "arguments": {"content": "secret"}}, "test"
        )
        self.assertEqual(r["status"], "denied")

    async def test_catalog_and_runtime_agree_on_confirmation(self):
        spec = self.core.tools.get("memory.forget")
        self.assertTrue(spec.public()["requires_confirmation"])

    async def test_local_policy_rejects_cloud_without_calling_it(self):
        from ev.ai.carlos_router import CarlosRouter
        from ev.ai.base import ProviderError, ProviderTurn

        local = AsyncMock()
        cloud = AsyncMock()
        local.begin.return_value = ProviderTurn("offline", "test", "local")
        router = CarlosRouter(local, cloud, lambda: "LOCAL ONLY")
        with self.assertRaises(ProviderError):
            await router.begin("use cloud: hello", [], [], [])
        cloud.begin.assert_not_called()
        r = await router.begin("Explain RAM", [], [], [])
        self.assertEqual(r.text, "local")

    async def test_mobile_transcription_is_bounded_and_mute_blocks_it(self):
        import base64

        self.core.voice.stt.transcribe = AsyncMock()
        with self.assertRaises(ValueError):
            await self.core.handle_request(
                {
                    "type": "carlos.voice.transcribe",
                    "payload": {"audio": base64.b64encode(b"\0" * 640002).decode()},
                }
            )
        self.core.voice.privacy_mode = True
        r = await self.core.handle_request(
            {
                "type": "carlos.voice.transcribe",
                "payload": {"audio": base64.b64encode(b"\0" * 6400).decode()},
            }
        )
        self.assertEqual(r["status"], "denied")
        self.core.voice.stt.transcribe.assert_not_called()

    async def test_greeting_bypasses_model_without_intercepting_commands(self):
        from ev.ai.carlos_router import CarlosRouter
        from ev.ai.base import ProviderTurn

        local = AsyncMock()
        local.begin.return_value = ProviderTurn("offline", "test", "routed")
        router = CarlosRouter(local, None, lambda: "NORMAL")
        self.assertEqual((await router.begin("Hello Carlos.", [], [], [])).text, "Yeah, I'm here.")
        local.begin.assert_not_called()
        self.assertEqual(
            (await router.begin("Hey Carlos, open Firefox", [], [], [])).text, "routed"
        )
        local.begin.assert_awaited_once()

    async def test_cancelling_preview_leaves_final_recognizer_available(self):
        import asyncio

        voice = self.core.voice
        self.assertIsNot(voice.stt, voice.preview_stt)
        self.assertNotEqual(
            voice.stt.config.get("server_port", 18082), voice.preview_stt.config["server_port"]
        )
        voice.stt.close = AsyncMock()
        entered = asyncio.Event()

        async def stalled(pcm):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await voice.preview_stt.close()

        voice.preview_stt.close = AsyncMock()
        voice.partial.transcribe = stalled
        voice.partial.feed(b"\0" * 48000, "preview-test")
        await entered.wait()
        await voice.partial.finish()
        voice.preview_stt.close.assert_awaited_once()
        voice.stt.close.assert_not_called()

    def test_interruption_is_not_sent_to_reasoning(self):
        from ev.voice.normalization import is_conversation_stop

        for phrase in ("Hold up.", "Carlos, wait.", "Wait a second", "Hold on"):
            self.assertTrue(is_conversation_stop(phrase), phrase)
        for phrase in (
            "Hold up the picture",
            "Wait for Firefox to open",
            "What does hold up mean?",
        ):
            self.assertFalse(is_conversation_stop(phrase), phrase)

    async def test_phone_speech_is_bounded_local_audio_without_desktop_playback(self):
        import base64, io, wave
        from ev.voice.tts import SynthesizedAudio

        self.core.voice.tts.synthesize = AsyncMock(
            return_value=SynthesizedAudio(b"\0" * 3200, 16000, 1, 2, "piper", "test", 12)
        )
        self.core.voice.speak = AsyncMock()
        with self.assertRaises(ValueError):
            await self.core.handle_request(
                {"type": "carlos.voice.synthesize", "payload": {"text": "x" * 121}}
            )
        self.core.voice.tts.synthesize.assert_not_called()
        r = await self.core.handle_request(
            {"type": "carlos.voice.synthesize", "payload": {"text": "Hello."}}
        )
        self.assertEqual(r["status"], "synthesized")
        self.assertFalse(r["audio_retained"])
        with wave.open(io.BytesIO(base64.b64decode(r["audio"]))) as w:
            self.assertEqual(w.getframerate(), 16000)
            self.assertEqual(w.getnframes(), 1600)
        self.core.voice.speak.assert_not_called()

    async def test_support_includes_timings_but_excludes_event_payloads(self):
        self.core.bus.publish(
            "command.received", "core", {"text": "PRIVATE_EVENT_CANARY"}, "bundle"
        )
        self.core.bus.publish(
            "tts.completed", "voice", {"text": "PRIVATE_EVENT_CANARY"}, "bundle", duration_ms=125
        )
        self.core.health_supervisor.components["TTS"] = {
            "state": "FAILED",
            "error": "PRIVATE_EVENT_CANARY",
        }
        result = self.core.holosystem.support()
        self.assertNotIn("PRIVATE_EVENT_CANARY", json.dumps(result))
        self.assertEqual(result["latency_samples"][-1]["stages_ms"]["tts_playback_total"], 125)
        self.assertEqual(result["health"]["TTS"], {"state": "FAILED"})

    async def test_workspace_restore_expands_before_executor_and_preserves_gaps(self):
        from unittest.mock import patch

        self.core._request_model_tool = AsyncMock(
            return_value={
                "status": "completed",
                "result": {"plan": {"steps": []}, "gaps": ["Missing editor"], "context_checks": []},
            }
        )
        self.core.planner.execute = AsyncMock(
            return_value={"status": "completed", "response": "Restored."}
        )
        from types import SimpleNamespace

        marker = SimpleNamespace(dry_run=False)
        with patch("ev.tools.plans.build_plan", return_value=marker):
            result = await self.core._submit_action_clauses_impl(
                "restore workspace coding", "restore-test"
            )
        self.core.planner.execute.assert_awaited_once_with(marker)
        self.assertEqual(result["workspace_gaps"], ["Missing editor"])
        self.assertIn("could not be restored", result["response"])

    async def test_unavailable_workspace_does_not_execute(self):
        self.core._request_model_tool = AsyncMock(
            return_value={
                "status": "completed",
                "result": {"plan": None, "gaps": ["Window closed"]},
            }
        )
        self.core.planner.execute = AsyncMock()
        result = await self.core._submit_action_clauses_impl(
            "restore workspace coding", "restore-test"
        )
        self.assertEqual(result["status"], "failed")
        self.core.planner.execute.assert_not_awaited()

    async def test_short_restore_uses_only_exact_saved_name(self):
        self.core.daily.save("workspace_layout", "bnm", {"windows": []})
        self.core._request_model_tool = AsyncMock(
            return_value={"status": "completed", "result": {"plan": None}}
        )
        result = await self.core._submit_action_clauses_impl("Restore BNM", "short-restore")
        self.core._request_model_tool.assert_awaited_once_with(
            {"name": "workspaces.restore_plan", "arguments": {"name": "BNM"}}, "short-restore"
        )
        self.assertEqual(result["status"], "failed")
