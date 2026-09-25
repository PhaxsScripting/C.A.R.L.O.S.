import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from ev.ai.nvidia import NvidiaProvider
from ev.ai.openai_responses import api_tool, normalize_wire_containers
from ev.paths import Paths
from ev.service import CarlosCore
from ev.tools.base import validate_schema, ValidationError


class CloudPlanSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = CarlosCore(
            paths=Paths(*(self.root / n for n in ("config", "data", "state", "cache", "runtime")))
        )
        self.addCleanup(self.service.memory.close)
        self.service.config["security"]["allowed_roots"] = [str(self.root)]

    def test_flexible_plan_and_control_schema_are_preserved_for_cloud(self):
        for name in ("agent.execute_plan", "agent.verify_conditions", "desktop.controls.set_state"):
            original = self.service.tools.get(name).public()
            wire = api_tool(original)
            self.assertFalse(wire["strict"], name)
            self.assertEqual(wire["parameters"], original["schema"])
            self.assertIsNot(wire["parameters"], original["schema"])
        step = api_tool(self.service.tools.get("agent.execute_plan").public())["parameters"][
            "properties"
        ]["steps"]["items"]
        self.assertNotEqual(step.get("additionalProperties"), False)

    def test_fixed_schema_still_uses_strict_conversion(self):
        wire = api_tool(self.service.tools.get("files.text.create").public())
        self.assertTrue(wire["strict"])
        self.assertFalse(wire["parameters"]["additionalProperties"])

    def test_nonfinite_model_values_do_not_bypass_numeric_limits(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValidationError):
                validate_schema(value, {"type": "number", "minimum": 0, "maximum": 100})

    def test_wire_normalization_does_not_coerce_text_booleans_or_unknown_arguments(self):
        schema = {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"type": "object"}},
                "text": {"type": "string"},
                "enabled": {"type": "boolean"},
            },
        }
        arguments = {
            "items": '[{"id": "one"}]',
            "text": '["keep literal"]',
            "enabled": "false",
            "unknown": "keep for rejection",
        }
        normalized = normalize_wire_containers(arguments, schema)
        self.assertEqual(normalized["items"], [{"id": "one"}])
        for key in ("text", "enabled", "unknown"):
            self.assertEqual(normalized[key], arguments[key])
        self.assertEqual(normalize_wire_containers("not json", {"type": "array"}), "not json")

    async def test_nvidia_wire_plan_executes_real_file_and_observed_hash(self):
        path = self.root / "cloud-plan-fixture.txt"
        arguments = {
            "goal": "Create the requested fixture with exact content",
            "steps": [
                {
                    "id": "create",
                    "tool": "files.text.create",
                    "arguments": {"path": str(path), "content": "fixture"},
                }
            ],
            "conditions": [
                {
                    "kind": "file_hash",
                    "path": str(path),
                    "sha256": hashlib.sha256(b"fixture").hexdigest(),
                }
            ],
        }

        def reply(name=None, args=None, text=None):
            message = {"content": text}
            if name:
                message["tool_calls"] = [
                    {
                        "id": name,
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ]
            return {
                "choices": [{"finish_reason": "tool_calls" if name else "stop", "message": message}]
            }

        provider = NvidiaProvider({"model": "test"})
        provider._request = AsyncMock(
            side_effect=[
                reply("ev__load_tools", {"names": ["agent.execute_plan"]}),
                reply(
                    "agent__execute_plan",
                    {
                        **arguments,
                        "steps": json.dumps(arguments["steps"]),
                        "conditions": json.dumps(arguments["conditions"]),
                    },
                ),
                reply(text="The requested file contents were verified."),
            ]
        )
        self.service.brain.provider = provider
        result = await self.service.brain.submit("Use a multi-step plan for this fixture")
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(path.read_bytes(), b"fixture")
        request = provider._request.await_args_list[1].args[0]
        wire = next(
            t["function"]
            for t in request["tools"]
            if t["function"]["name"] == "agent__execute_plan"
        )
        self.assertEqual(wire["parameters"]["properties"]["steps"]["items"], {"type": "object"})
        messages = provider._request.await_args.args[0]["messages"]
        receipt = json.loads(next(m["content"] for m in reversed(messages) if m["role"] == "tool"))
        self.assertTrue(receipt["result"]["verified"])
        self.assertFalse(receipt["result"]["goal_verified"])
