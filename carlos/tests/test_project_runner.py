import asyncio
import hashlib
import logging
import os
import shutil
import tempfile
import socket
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from ev.events import PhaxEventBus
from ev.tools import ToolContext, ToolRegistry
from ev.tools.base import ValidationError
from ev.tools.projects import (
    inspect_project,
    project_command,
    diagnose_output,
    register_project_tools,
)
from ev.tools.builtin import build_project
from ev.ai.base import ProviderTurn, ToolCall
from ev.ai.nvidia import NvidiaProvider
from ev.paths import Paths
from ev.service import CarlosCore


class ProjectRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "pyproject.toml").write_text('[project]\nname="fixture"\n')
        self.context = ToolContext(
            {"security": {"allowed_roots": [str(self.root)], "max_tool_output_bytes": 20000}},
            PhaxEventBus(),
            logging.getLogger("test-project"),
        )
        self.registry = ToolRegistry(self.context)
        self.state = self.root / "runs"
        register_project_tools(self.registry, self.state)
        self.args = {
            "project": str(self.project),
            "build_system": "python",
            "operation": "test",
            "expected_manifest_sha256": inspect_project(
                {"project": str(self.project)}, self.context
            )["manifest_sha256"],
        }

    async def execute(self):
        spec, args = self.registry.validate("development.project.run", self.args)
        return await self.registry.execute(spec, args)

    def test_manifest_inspection_never_executes_project(self):
        result = inspect_project({"project": str(self.project)}, self.context)
        self.assertEqual(result["build_systems"], ["python"])
        self.assertFalse(result["sandbox_verified_for_this_project"])
        self.assertTrue(self.registry.get("development.project.run").requires_confirmation)
        self.assertTrue(self.registry.get("development.project.inspect").read_only)

    def test_legacy_build_cannot_bypass_approval_and_sandbox(self):
        with patch(
            "ev.tools.builtin.run_command",
            side_effect=AssertionError("Legacy path must not execute"),
        ):
            result = build_project({"project": str(self.project)}, self.context)
        self.assertFalse(result["ok"])
        self.assertFalse(result["executed"])
        self.assertIn("development.project.run", result["replacement_tools"])

    async def test_successful_approved_replacement_resolves_retired_build_failure(self):
        import time

        service = CarlosCore(
            paths=Paths(*(self.root / n for n in ("config", "data", "state", "cache", "runtime")))
        )
        self.addCleanup(service.memory.close)
        old = ToolCall("old", "development.build_project", {"project": str(self.project)})
        replacement = ToolCall(
            "new", "development.project.run", {"project": str(self.project), "operation": "build"}
        )
        failure = {"status": "failed", "result": build_project(old.arguments, self.context)}
        succeeded = {"status": "completed", "execution": {"verified": True}}
        service.brain._tool_history["repaired"] = [(old, failure), (replacement, succeeded)]
        result = await service.brain._complete(
            ProviderTurn("test", "test", "Built"), "repaired", time.monotonic()
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["recovered_failures"], 1)
        replacement.arguments["project"] = str(self.root / "different-project")
        service.brain._tool_history["wrong"] = [(old, failure), (replacement, succeeded)]
        result = await service.brain._complete(
            ProviderTurn("test", "test", "Built"), "wrong", time.monotonic()
        )
        self.assertEqual(result["status"], "failed")

    async def test_model_project_execution_is_gated_until_explicit_approval(self):
        service = CarlosCore(
            paths=Paths(*(self.root / n for n in ("config", "data", "state", "cache", "runtime")))
        )
        self.addCleanup(service.memory.close)
        service.config["security"]["allowed_roots"] = [str(self.root)]
        provider = NvidiaProvider({"model": "test"})
        provider._request = AsyncMock(side_effect=AssertionError("No network calls"))
        provider.begin = AsyncMock(
            return_value=ProviderTurn(
                "nvidia", "test", "", [ToolCall("build", "development.project.run", self.args)]
            )
        )
        provider.continue_with_tools = AsyncMock(
            return_value=ProviderTurn("nvidia", "test", "The process exited successfully.")
        )
        service.brain.provider = provider
        execute = AsyncMock(return_value={"ok": True, "verified": True, "exit_code": 0})
        with patch.object(service.tools, "execute", execute):
            result = await service.brain.submit("Run the tests for that project")
            self.assertEqual(result["status"], "confirmation_required")
            execute.assert_not_awaited()
            pending = result["confirmation"]
            confirmed = await service.resolve_confirmation(
                {"id": pending["id"], "approval_token": pending["approval_token"], "approved": True}
            )
            self.assertEqual(confirmed["command"]["status"], "completed")
            execute.assert_awaited_once()
            call, receipt = provider.continue_with_tools.await_args.args[1][0]
            self.assertEqual(call.name, "development.project.run")
            self.assertFalse(receipt.get("goal_verified", False))

    def test_stale_manifest_refused(self):
        (self.project / "pyproject.toml").write_text("changed")
        with self.assertRaisesRegex(ValidationError, "changed"):
            project_command(self.args, self.context)

    def test_root_scope_and_linked_manifest_refused(self):
        with self.assertRaises(ValidationError):
            inspect_project({"project": str(self.root)}, self.context)
        manifest = self.project / "pyproject.toml"
        manifest.unlink()
        manifest.symlink_to(self.root / "elsewhere")
        with self.assertRaises(ValidationError):
            inspect_project({"project": str(self.project)}, self.context)

    def test_environment_and_network_are_not_inherited(self):
        with patch.dict(
            os.environ,
            {"NVIDIA_API_KEY": "not-for-child", "DBUS_SESSION_BUS_ADDRESS": "private-bus"},
        ):
            _, _, command = project_command(self.args, self.context)
        self.assertIn("--unshare-all", command)
        self.assertIn("--clearenv", command)
        self.assertNotIn("not-for-child", str(command))
        self.assertNotIn("private-bus", str(command))

    def test_dotenv_project_requires_sanitized_workspace(self):
        (self.project / ".env").write_text("PRIVATE=value")
        with self.assertRaisesRegex(ValidationError, "sanitized"):
            project_command(self.args, self.context)

    def test_run_cannot_target_executable_outside_project(self):
        self.args.update(operation="run", executable="/usr/bin/true")
        with self.assertRaises(ValidationError):
            project_command(self.args, self.context)

    def test_project_hardlink_to_external_file_is_not_writable_through_sandbox(self):
        external = self.root / "external"
        external.write_text("preserve")
        os.link(external, self.project / "link")
        with self.assertRaisesRegex(ValidationError, "hardlinked"):
            project_command(self.args, self.context)
        self.assertEqual(external.read_text(), "preserve")

    def test_project_unix_socket_cannot_expose_host_service(self):
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(self.project / "host-service.sock"))
            with self.assertRaisesRegex(ValidationError, "socket"):
                project_command(self.args, self.context)

    def test_error_parser_limits_to_project_and_first_error(self):
        text = f"{self.project}/main.cpp:7:4: error: unknown type\n/usr/include/a.h:2: error: cascade\n"
        result = diagnose_output(text, self.project)
        self.assertEqual(result["primary_diagnostic"]["line"], 7)
        self.assertFalse(result["root_cause_verified"])
        self.assertEqual(result["additional_diagnostics"], [])

    @unittest.skipUnless(shutil.which("bwrap"), "Bubblewrap is not installed")
    async def test_real_sandbox_cannot_read_parent_secret_or_inherit_key(self):
        (self.root / "outside-secret").write_text("do-not-read")
        tests = self.project / "tests"
        tests.mkdir()
        (tests / "test_isolation.py").write_text(
            "import unittest, os\nfrom pathlib import Path\nclass T(unittest.TestCase):\n"
            " def test_isolation(self):\n"
            f"  self.assertFalse(Path({str(self.root / 'outside-secret')!r}).exists())\n"
            "  self.assertNotIn('NVIDIA_API_KEY', os.environ)\n"
            "  self.assertNotIn('DBUS_SESSION_BUS_ADDRESS', os.environ)\n"
            "  Path('artifact.txt').write_text('sandbox fixture')\n"
        )
        with patch.dict(os.environ, {"NVIDIA_API_KEY": "do-not-inherit"}):
            result = await self.execute()
        self.assertTrue(result["ok"], result)
        self.assertEqual((self.project / "artifact.txt").read_text(), "sandbox fixture")
        self.assertEqual(Path(result["log_path"]).stat().st_mode & 0o777, 0o600)
        spec, args = self.registry.validate(
            "development.project.result", {"run_id": result["run_id"]}
        )
        saved = await self.registry.execute(spec, args)
        self.assertTrue(saved["historical"])
        self.assertEqual(saved["run"]["exit_code"], 0)
        self.assertEqual(
            (self.state / result["run_id"] / "result.json").stat().st_mode & 0o777, 0o600
        )

    async def test_cancellation_during_process_creation_still_cleans_exact_child(self):
        entered, release = asyncio.Event(), asyncio.Event()
        process = type("FakeProcess", (), {"pid": 987654321, "returncode": None})()

        async def wait():
            process.returncode = -9

        process.wait = AsyncMock(side_effect=wait)

        async def spawn(*args, **kwargs):
            entered.set()
            await release.wait()
            return process

        with patch("ev.tools.projects.asyncio.create_subprocess_exec", side_effect=spawn), patch(
            "ev.tools.projects.os.killpg"
        ) as kill:
            task = asyncio.create_task(self.execute())
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            kill.assert_called_once()
            self.assertEqual(kill.call_args.args[0], process.pid)
            process.wait.assert_awaited_once()

    async def test_missing_run_and_path_traversal_cannot_read_arbitrary_logs(self):
        with self.assertRaises(ValidationError):
            self.registry.validate("development.project.result", {"run_id": "../secrets"})
        spec, args = self.registry.validate("development.project.result", {"run_id": "f" * 32})
        with self.assertRaisesRegex(ValidationError, "complete saved result"):
            await self.registry.execute(spec, args)

    @unittest.skipUnless(shutil.which("bwrap"), "Bubblewrap is not installed")
    async def test_timeout_kills_owned_project_runner(self):
        script = self.project / "sleep.py"
        script.write_text("#!/usr/bin/python3\nimport time\ntime.sleep(30)\n")
        script.chmod(0o700)
        self.args.update(operation="run", executable=str(script), timeout_seconds=1)
        result = await self.execute()
        self.assertFalse(result["ok"])
        self.assertTrue(result["timed_out"])

    @unittest.skipUnless(shutil.which("bwrap"), "Bubblewrap is not installed")
    async def test_real_compiler_failure_has_source_location(self):
        (self.project / "CMakeLists.txt").write_text(
            "cmake_minimum_required(VERSION 3.16)\nproject(Fixture C)\nadd_executable(fixture main.c)\n"
        )
        (self.project / "main.c").write_text("int main(void) { invalid syntax; }\n")
        self.args.update(
            build_system="cmake",
            operation="configure",
            expected_manifest_sha256=inspect_project({"project": str(self.project)}, self.context)[
                "manifest_sha256"
            ],
        )
        configured = await self.execute()
        self.assertTrue(configured["ok"], configured)
        self.args["operation"] = "build"
        result = await self.execute()
        self.assertFalse(result["ok"])
        self.assertEqual(result["primary_diagnostic"]["path"], str(self.project / "main.c"))
