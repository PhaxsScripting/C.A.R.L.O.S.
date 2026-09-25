from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

from ev.ai.base import ProviderError
from ev.ai.local_llama import LocalLlamaProvider, _boot_id, _process_identity
from ev.config import DEFAULT_CONFIG


def argument(command: list[str], flag: str) -> str:
    return command[command.index(flag) + 1]


class LocalInferenceResourcesTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def ownership_record(owner: dict | None = None, server: dict | None = None) -> dict:
        identity = _process_identity(os.getpid())
        assert identity is not None
        return {
            "version": 1,
            "component": "ev.local_llama",
            "boot_id": _boot_id(),
            "owner": dict(owner or identity),
            "server": {
                **dict(server or identity),
                "host": "127.0.0.1",
                "port": 18080,
                "binary": "/test/llama-server",
                "model_path": "/test/model.gguf",
            },
        }

    async def test_managed_server_starts_with_desktop_cpu_budget(self) -> None:
        provider = LocalLlamaProvider(DEFAULT_CONFIG["providers"]["local_llama"])
        process = Mock(returncode=None, wait=AsyncMock(return_value=0))
        with (
            patch(
                "ev.telemetry.read_temperature", return_value={"celsius": 50.0, "sensor": "test"}
            ),
            patch.object(provider, "_healthy", new=AsyncMock(side_effect=[False, False, True])),
            patch.object(
                LocalLlamaProvider,
                "available",
                new_callable=PropertyMock,
                return_value=(True, "test"),
            ),
            patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)) as spawn,
        ):
            await provider._ensure_server()
        command = list(spawn.call_args.args)
        self.assertEqual(command[:3], ["/usr/bin/nice", "-n", "15"])
        self.assertEqual(argument(command, "--threads"), "3")
        self.assertEqual(argument(command, "--threads-batch"), "3")
        self.assertEqual(argument(command, "--threads-http"), "2")
        self.assertEqual(argument(command, "--poll"), "0")
        self.assertEqual(argument(command, "--poll-batch"), "0")
        self.assertEqual(argument(command, "--parallel"), "1")
        self.assertIn("--no-op-offload", command)
        await provider.close()
        process.terminate.assert_called_once()
        process.wait.assert_awaited_once()

    def test_unset_batch_threads_follow_the_selected_generation_budget(self) -> None:
        provider = LocalLlamaProvider({"threads": 2})
        self.assertEqual(argument(provider._server_command(), "--threads-batch"), "2")

    def test_zero_gpu_layers_disables_separate_operation_offload(self):
        self.assertIn("--no-op-offload", LocalLlamaProvider({"gpu_layers": 0})._server_command())
        self.assertNotIn(
            "--no-op-offload", LocalLlamaProvider({"gpu_layers": 10})._server_command()
        )
        self.assertIn(
            "--no-op-offload",
            LocalLlamaProvider({"gpu_layers": 10, "op_offload": False})._server_command(),
        )
        self.assertNotIn(
            "--no-op-offload",
            LocalLlamaProvider({"gpu_layers": 0, "op_offload": True})._server_command(),
        )

    def test_zero_threads_cannot_enable_unbounded_auto_cpu_selection(self) -> None:
        provider = LocalLlamaProvider({"threads": 0, "threads_batch": -1, "threads_http": -1})
        command = provider._server_command()
        for flag in ("--threads", "--threads-batch", "--threads-http"):
            self.assertEqual(argument(command, flag), "1")

    async def test_connecting_to_external_server_never_terminates_it(self) -> None:
        provider = LocalLlamaProvider({})
        with (
            patch.object(provider, "_healthy", new=AsyncMock(return_value=True)),
            patch("asyncio.create_subprocess_exec", new=AsyncMock()) as spawn,
        ):
            await provider._ensure_server()
            await provider.close()
        spawn.assert_not_awaited()

    def test_ownership_record_is_atomic_private_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "llama-owner.json"
            provider = LocalLlamaProvider({}, ownership_path=path)
            record = self.ownership_record()
            provider._write_ownership_record(record)

            loaded, error = provider._read_ownership_record()
            self.assertIsNone(error)
            self.assertEqual(loaded, record)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*")), [])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), record)

    async def test_managed_endpoint_never_trusts_or_kills_unowned_health_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = LocalLlamaProvider({}, ownership_path=Path(directory) / "llama-owner.json")
            with (
                patch.object(provider, "_healthy", new=AsyncMock(return_value=True)),
                patch.object(provider, "_terminate_recorded_server", new=AsyncMock()) as terminate,
                patch("asyncio.create_subprocess_exec", new=AsyncMock()) as spawn,
            ):
                with self.assertRaisesRegex(ProviderError, "unowned service"):
                    await provider._ensure_server()
            terminate.assert_not_awaited()
            spawn.assert_not_awaited()

    async def test_live_recorded_owner_blocks_reclamation(self) -> None:
        provider = LocalLlamaProvider({}, ownership_path="/unused/llama-owner.json")
        record = self.ownership_record()
        with (
            patch.object(provider, "_read_ownership_record", return_value=(record, None)),
            patch("ev.ai.local_llama._identity_matches", return_value=True),
            patch.object(provider, "_terminate_recorded_server", new=AsyncMock()) as terminate,
            patch.object(provider, "_healthy", new=AsyncMock()) as healthy,
        ):
            with self.assertRaisesRegex(ProviderError, "another live E.V. core"):
                await provider._prepare_managed_endpoint()
        terminate.assert_not_awaited()
        healthy.assert_not_awaited()

    async def test_dead_owner_reclaims_only_exact_recorded_server(self) -> None:
        provider = LocalLlamaProvider({}, ownership_path="/unused/llama-owner.json")
        record = self.ownership_record()
        matches = lambda identity, _boot: identity is record["server"]
        with (
            patch.object(provider, "_read_ownership_record", return_value=(record, None)),
            patch("ev.ai.local_llama._identity_matches", side_effect=matches),
            patch.object(
                provider, "_terminate_recorded_server", new=AsyncMock(return_value=True)
            ) as terminate,
            patch.object(provider, "_remove_ownership_record") as remove,
            patch.object(provider, "_healthy", new=AsyncMock(return_value=False)),
        ):
            self.assertFalse(await provider._prepare_managed_endpoint())
        terminate.assert_awaited_once_with(record)
        remove.assert_called_once_with(record)

    async def test_dead_owner_does_not_signal_a_replaced_server_pid(self) -> None:
        provider = LocalLlamaProvider({}, ownership_path="/unused/llama-owner.json")
        record = self.ownership_record()
        with (
            patch.object(provider, "_read_ownership_record", return_value=(record, None)),
            patch("ev.ai.local_llama._identity_matches", return_value=False),
            patch.object(provider, "_terminate_recorded_server", new=AsyncMock()) as terminate,
            patch.object(provider, "_remove_ownership_record") as remove,
            patch.object(provider, "_healthy", new=AsyncMock(return_value=False)),
        ):
            self.assertFalse(await provider._prepare_managed_endpoint())
        terminate.assert_not_awaited()
        remove.assert_called_once_with(record)

    async def test_managed_close_signals_only_matching_owned_child_and_removes_record(self) -> None:
        provider = LocalLlamaProvider({}, ownership_path="/unused/llama-owner.json")
        process = Mock(returncode=None, wait=AsyncMock(return_value=0))
        record = self.ownership_record()
        provider._process = process
        provider._owns_process = True
        provider._server_identity = dict(record["server"])
        provider._ownership_record = record
        with (
            patch("ev.ai.local_llama._identity_matches", return_value=True),
            patch.object(provider, "_remove_ownership_record") as remove,
        ):
            await provider.close()
        process.terminate.assert_called_once()
        process.wait.assert_awaited_once()
        remove.assert_called_once_with(record)

    async def test_managed_close_never_signals_mismatched_pid(self) -> None:
        provider = LocalLlamaProvider({}, ownership_path="/unused/llama-owner.json")
        process = Mock(returncode=None, wait=AsyncMock(return_value=0))
        record = self.ownership_record()
        provider._process = process
        provider._owns_process = True
        provider._server_identity = dict(record["server"])
        provider._ownership_record = record
        with (
            patch("ev.ai.local_llama._identity_matches", return_value=False),
            patch.object(provider, "_remove_ownership_record") as remove,
        ):
            await provider.close()
        process.terminate.assert_not_called()
        process.kill.assert_not_called()
        process.wait.assert_not_awaited()
        remove.assert_not_called()


if __name__ == "__main__":
    unittest.main()
