import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from ev.ai.base import ProviderError
from ev.ai.local_agent import LocalAgentProvider, matching_tools, seed_tools
from ev.config import load_config
from ev.paths import Paths
from ev.service import CarlosCore


def reply(name=None, args=None, text=""):
    message = {"role": "assistant", "content": text}
    if name:
        message["tool_calls"] = [
            {
                "id": name,
                "type": "function",
                "function": {"name": name.replace(".", "__"), "arguments": json.dumps(args)},
            }
        ]
    return {"choices": [{"finish_reason": "tool_calls" if name else "stop", "message": message}]}


class LocalAgentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = CarlosCore(
            paths=Paths(*(self.root / n for n in ("config", "data", "state", "cache", "runtime")))
        )
        self.addCleanup(self.service.memory.close)
        self.service.config["security"]["allowed_roots"] = [str(self.root)]
        self.provider = LocalAgentProvider({"model": "test"})
        self.catalog = self.service.tools.catalog()

    def test_service_constructs_local_agent_without_cloud_credentials(self):
        paths = Paths(
            *(self.root / "factory" / n for n in ("config", "data", "state", "cache", "runtime"))
        )
        config = load_config(paths)
        config["providers"]["active"] = "local_agent"
        paths.config_file.write_text(json.dumps(config))
        candidate = CarlosCore(paths=paths)
        try:
            self.assertIsInstance(candidate.brain.provider, LocalAgentProvider)
            self.assertIsNotNone(candidate.brain.provider._ownership_path)
            self.assertTrue(candidate.brain.provider.interprets_all_requests)
        finally:
            candidate.memory.close()

    def test_new_tool_is_searchable_from_registry_metadata(self):
        catalog = {t["name"]: t for t in self.catalog}
        catalog["new_domain.palette"] = {
            "name": "new_domain.palette",
            "description": "Inspect periwinkle palette swatches",
        }
        self.assertEqual(matching_tools("periwinkle swatches", catalog)[0], "new_domain.palette")

    def test_filename_and_path_words_do_not_seed_note_or_workspace_actions(self):
        catalog = {t["name"]: t for t in self.catalog if t["name"] != "agent.execute_plan"}
        query = "Inside /tmp/workspace, make a folder named work, and inside that folder create note.txt containing exactly hello, with no newline. Check the file contents afterward."
        self.assertEqual(
            set(seed_tools(query, catalog)), {"files.text.create", "files.directory.create"}
        )
        self.assertEqual(
            seed_tools(query.replace("note.txt", "spotify.txt"), catalog),
            seed_tools(query, catalog),
        )

    def test_operation_and_target_coverage_outranks_namespace_only(self):
        catalog = {t["name"]: t for t in self.catalog}
        self.assertEqual(
            matching_tools("Unpause my Spotify, bro, what the fuck.", catalog)[0], "audio.media"
        )
        self.assertEqual(
            seed_tools("Unpause my Spotify, bro, what the fuck.", catalog), ["audio.media"]
        )

    def test_seed_pruning_does_not_hide_complementary_capabilities(self):
        catalog = {
            "one.paint": {"description": "blue canvas"},
            "two.listen": {"description": "music audio"},
        }
        self.assertEqual(set(seed_tools("blue canvas and music audio", catalog)), set(catalog))

    def test_casual_topic_does_not_seed_tools_from_one_incidental_word(self):
        catalog = {t["name"]: t for t in self.catalog}
        self.assertEqual(seed_tools("I had a rough day", catalog), [])
        self.assertEqual(seed_tools("What fruit did I say I liked?", catalog), [])
        self.assertEqual(
            seed_tools("Could you help me create a defensive cybersecurity tool?", catalog), []
        )
        self.assertEqual(seed_tools("audio.media", catalog), ["audio.media"])

    def test_supported_browser_enum_is_searchable_without_hardcoded_aliases(self):
        catalog = {t["name"]: t for t in self.catalog}
        self.assertIn(
            "browser.open_url", matching_tools("Open youtube.com in Firefox", catalog)[:2]
        )
        catalog["fictional.viewer"] = {
            "name": "fictional.viewer",
            "description": "Choose a viewer",
            "schema": {
                "type": "object",
                "properties": {"viewer": {"type": "string", "enum": ["zorblax"]}},
            },
        }
        self.assertEqual(matching_tools("zorblax", catalog)[0], "fictional.viewer")

    async def test_discovers_new_registered_setting_without_phrase_parser(self):
        self.provider._post = AsyncMock(
            side_effect=[
                reply("agent.load_tools", {"names": ["settings.power_profile.get"]}),
                reply("settings.power_profile.get", {}),
            ]
        )
        turn = await self.provider.begin(
            "Which energy modes are supported here?", [], [], self.catalog
        )
        self.assertEqual(turn.tool_calls[0].name, "settings.power_profile.get")
        first = self.provider._post.await_args_list[0].args
        self.assertEqual(
            [t["name"] for t in first[1]][-2:], ["agent.find_tools", "agent.load_tools"]
        )
        self.assertLessEqual(len(first[1]), 4)
        self.assertIn("settings", first[0][0]["content"])
        self.assertNotIn("settings.power_profile.get", first[0][0]["content"])
        self.assertNotIn("system.power,", first[0][0]["content"])

    async def test_long_workflow_rotates_definitions_without_execution_or_history_loss(self):
        definitions = [
            {
                "name": f"fixture.operation{i}",
                "description": "Read fixture state",
                "permission": "SAFE",
                "schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
            }
            for i in range(20)
        ]
        self.provider._post = AsyncMock(
            side_effect=[
                reply("agent.load_tools", {"names": ["fixture.operation19", "fixture.operation0"]}),
                reply("fixture.operation19", {}),
            ]
        )
        messages = [
            {"role": "user", "content": "Continue the observed workflow"},
            {
                "role": "tool",
                "tool_call_id": "old",
                "content": '{"verified":true,"receipt":"retain-me"}',
            },
        ]
        turn = await self.provider._bounded_discover(
            messages, definitions, [t["name"] for t in definitions[:16]]
        )
        self.assertEqual(turn.tool_calls[0].name, "fixture.operation19")
        selected = turn.continuation["selected_names"]
        self.assertEqual(len(selected), 16)
        self.assertIn("fixture.operation0", selected)  # Explicit reload refreshes recency.
        self.assertIn("fixture.operation19", selected)
        self.assertNotIn("fixture.operation1", selected)
        second_messages, exposed = self.provider._post.await_args.args
        self.assertLessEqual(len(exposed), 18)  # Sixteen real tools plus discovery.
        self.assertTrue(any("retain-me" in m.get("content", "") for m in second_messages))
        receipt = json.loads(second_messages[-1]["content"])
        self.assertEqual(receipt["actions_executed"], 0)
        self.assertEqual(receipt["unloaded_definitions"], ["fixture.operation1"])

    async def test_definition_rotation_does_not_admit_unknown_tools(self):
        self.provider._post = AsyncMock(
            return_value=reply("agent.load_tools", {"names": ["not.registered"]})
        )
        with self.assertRaises(ProviderError):
            await self.provider._bounded_discover(
                [{"role": "user", "content": "Continue"}],
                self.catalog,
                [t["name"] for t in self.catalog[:16]],
            )
        self.assertEqual(self.provider._post.await_count, 1)

    async def test_service_preserves_unresolved_multi_step_request(self):
        self.service.brain.provider = self.provider
        self.service.brain.submit = AsyncMock(
            return_value={"status": "completed", "response": "test"}
        )
        text = "open the thing we discussed and arrange the result for comparison"
        with patch.object(self.service.planner, "try_plan", return_value=None):
            await self.service._submit_action_clauses_impl(text, "whole-request")
        self.service.brain.submit.assert_awaited_once_with(text, "whole-request")

    async def test_local_agent_preserves_existing_fast_execution_path(self):
        self.service.brain.provider = self.provider
        self.provider._post = AsyncMock(side_effect=AssertionError("No inference needed"))
        self.service.tools.execute = AsyncMock(return_value={"verified": True})
        result = await self.service._submit_action_clauses("pause Spotify")
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(self.service.tools.execute.await_args.args[0].name, "audio.media")
        self.provider._post.assert_not_awaited()

    async def test_conversation_can_answer_without_discovery_or_effects(self):
        self.provider._post = AsyncMock(return_value=reply(text="That sounds frustrating."))
        turn = await self.provider.begin("I had a rough day", [], [], self.catalog)
        self.assertEqual(turn.tool_calls, [])
        self.assertEqual(self.provider._post.await_count, 1)

    async def test_unexecuted_promise_gets_one_bounded_model_correction(self):
        self.provider._post = AsyncMock(
            side_effect=[
                reply(text="I'm going to open the YouTube website using your Firefox browser."),
                reply("browser.open_url", {"url": "https://youtube.com", "browser": "firefox"}),
            ]
        )
        turn = await self.provider.begin("Open youtube.com in Firefox", [], [], self.catalog)
        self.assertEqual(turn.tool_calls[0].name, "browser.open_url")
        self.assertEqual(self.provider._post.await_count, 2)
        self.assertIn("Nothing has executed", self.provider._post.await_args.args[0][-1]["content"])

    async def test_repeated_promise_does_not_create_an_infinite_retry(self):
        self.provider._post = AsyncMock(return_value=reply(text="I'll open Firefox."))
        turn = await self.provider.begin("Open youtube.com in Firefox", [], [], self.catalog)
        self.assertFalse(turn.tool_calls)
        self.assertEqual(self.provider._post.await_count, 2)

    async def test_completed_action_is_never_replayed_to_repair_final_prose(self):
        self.provider._post = AsyncMock(
            side_effect=[
                reply("audio.media", {"action": "play", "player": "spotify"}),
                reply(text="I resumed Spotify."),
            ]
        )
        turn = await self.provider.begin("Unpause my Spotify", [], [], self.catalog)
        result = await self.provider.continue_with_tools(
            turn,
            [(turn.tool_calls[0], {"status": "completed", "result": {"verified": True}})],
            self.catalog,
        )
        self.assertFalse(result.tool_calls)
        self.assertEqual(self.provider._post.await_count, 2)

    async def test_prewarm_uses_real_discovery_prefix_without_execution(self):
        self.provider._post = AsyncMock(return_value=reply(text="OK"))
        await self.provider.prewarm_with_tools(self.catalog)
        warm_messages, warm_tools = self.provider._post.await_args.args
        self.assertEqual([t["name"] for t in warm_tools], ["agent.find_tools", "agent.load_tools"])
        await self.provider.begin("I had a rough day", [], [], self.catalog)
        messages, tools = self.provider._post.await_args.args
        self.assertEqual(messages[0], warm_messages[0])
        self.assertEqual(tools, warm_tools)

    async def test_disabled_prewarm_does_not_load_model(self):
        self.provider.config["prewarm"] = False
        self.provider._post = AsyncMock()
        await self.provider.prewarm_with_tools(self.catalog)
        self.provider._post.assert_not_awaited()

    async def test_service_passes_catalog_to_candidate_warmup(self):
        self.provider.prewarm_with_tools = AsyncMock()
        self.service.brain.provider = self.provider
        self.service.voice.prewarm = AsyncMock()
        await self.service._prewarm_response_stack()
        self.provider.prewarm_with_tools.assert_awaited_once_with(self.catalog)

    async def test_continuation_bounds_large_evidence_as_valid_marked_json(self):
        self.provider._post = AsyncMock(
            side_effect=[
                reply("agent.load_tools", {"names": ["files.list"]}),
                reply("files.list", {"path": str(self.root)}),
                reply(text="I need a narrower observation."),
            ]
        )
        turn = await self.provider.begin("Inspect this folder", [], [], self.catalog)
        result = {
            "status": "completed",
            "result": {"entries": [{"name": "long" * 1000} for _ in range(100)]},
        }
        await self.provider.continue_with_tools(turn, [(turn.tool_calls[0], result)], self.catalog)
        messages = self.provider._post.await_args.args[0]
        output = next(item["content"] for item in reversed(messages) if item["role"] == "tool")
        self.assertLessEqual(len(output), 6000)
        self.assertTrue(json.loads(output)["context_truncated"])

    async def test_search_loads_real_schemas_without_full_catalog_in_prompt(self):
        self.provider._post = AsyncMock(
            side_effect=[
                reply("agent.find_tools", {"query": "power profile"}),
                reply("settings.power_profile.get", {}),
            ]
        )
        turn = await self.provider.begin(
            "Which energy modes are supported here?", [], [], self.catalog
        )
        self.assertEqual(turn.tool_calls[0].name, "settings.power_profile.get")
        loaded = self.provider._post.await_args.args[1]
        spec = next(t for t in loaded if t["name"] == "settings.power_profile.get")
        self.assertEqual(spec, self.service.tools.get(spec["name"]).public())
        # Two initial schemas, up to four discovered schemas, two discovery tools.
        self.assertLessEqual(len(loaded), 8)

    async def test_rejects_unknown_blocked_and_duplicate_discovery(self):
        for names in (["invented.run"], ["system.power"], ["files.hash", "files.hash"]):
            self.provider._post = AsyncMock(
                return_value=reply("agent.load_tools", {"names": names})
            )
            with self.assertRaises(ProviderError):
                await self.provider.begin("do it", [], [], self.catalog)

    async def test_mixed_discovery_and_effects_never_leave_adapter(self):
        raw = reply("agent.load_tools", {"names": ["files.hash"]})
        raw["choices"][0]["message"]["tool_calls"] *= 2
        self.provider._post = AsyncMock(return_value=raw)
        with self.assertRaisesRegex(ProviderError, "mixed"):
            await self.provider.begin("do it", [], [], self.catalog)

    async def test_repeated_discovery_is_bounded(self):
        self.provider._post = AsyncMock(
            return_value=reply("agent.load_tools", {"names": ["files.hash"]})
        )
        with self.assertRaisesRegex(ProviderError, "bounded rounds"):
            await self.provider.begin("do it", [], [], self.catalog)
        self.assertEqual(self.provider._post.await_count, 4)

    async def test_discovery_cancellation_propagates(self):
        self.provider._post = AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await self.provider.begin("do it", [], [], self.catalog)

    async def test_local_plan_uses_real_executor_and_observed_hash(self):
        target = self.root / "local-fixture.txt"
        arguments = {
            "goal": "Create fixture with verified bytes",
            "steps": [
                {
                    "id": "create",
                    "tool": "files.text.create",
                    "arguments": {"path": str(target), "content": "fixture"},
                }
            ],
            "conditions": [
                {
                    "kind": "file_hash",
                    "path": str(target),
                    "sha256": hashlib.sha256(b"fixture").hexdigest(),
                }
            ],
        }
        self.provider._post = AsyncMock(
            side_effect=[
                reply(
                    "agent.load_tools",
                    {"names": ["agent.execute_plan", "files.text.create", "files.hash"]},
                ),
                reply("agent.execute_plan", {**arguments, "steps": json.dumps(arguments["steps"])}),
                reply(text="The requested file contents were verified."),
            ]
        )
        self.service.brain.provider = self.provider
        result = await self.service.brain.submit("Use a multi-step plan for this fixture")
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(target.read_bytes(), b"fixture")
        messages = self.provider._post.await_args.args[0]
        receipt = json.loads(next(m["content"] for m in reversed(messages) if m["role"] == "tool"))
        self.assertTrue(receipt["result"]["verified"])
        self.assertFalse(receipt["result"]["goal_verified"])

    async def test_forged_success_does_not_hide_failed_goal(self):
        target = self.root / "missing.txt"
        arguments = {
            "goal": "Verify a nonexistent file",
            "steps": [{"id": "hash", "tool": "files.hash", "arguments": {"path": str(target)}}],
            "conditions": [{"kind": "file_hash", "path": str(target), "sha256": "0" * 64}],
        }
        self.provider._post = AsyncMock(
            side_effect=[
                reply("agent.load_tools", {"names": ["agent.execute_plan"]}),
                reply("agent.execute_plan", arguments),
                reply(text="Everything succeeded."),
            ]
        )
        self.service.brain.provider = self.provider
        result = await self.service.brain.submit("Use a multi-step plan for this missing fixture")
        self.assertNotEqual(result["status"], "completed", result)
        self.assertFalse(target.exists())
