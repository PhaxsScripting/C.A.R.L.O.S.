import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from ev.plugins import load_enabled_plugins
from ev.permissions import Permission
from ev.tools.base import ToolRegistry, ToolSpec, ValidationError


class PluginTests(unittest.IsolatedAsyncioTestCase):
    def registry(self):
        return ToolRegistry(
            SimpleNamespace(
                config={
                    "carlos": {"strict_permissions": True},
                    "security": {"max_tool_output_bytes": 4096},
                },
                capability_probe=AsyncMock(
                    return_value={"capabilities": {"DesktopControl": {"state": "UNVERIFIED"}}}
                ),
            )
        )

    def spec(self):
        return ToolSpec(
            "plugin.fixture.observe",
            "FIXTURE",
            "Observe a fixture",
            Permission.SAFE,
            {"type": "object"},
            AsyncMock(return_value={"ok": True}),
            output_schema={
                "type": "object",
                "required": ["ok"],
                "properties": {"ok": {"type": "boolean"}},
            },
            offline_available=True,
            reversible=True,
        )

    def point(self, specs):
        return SimpleNamespace(name="fixture", load=MagicMock(return_value=lambda: specs))

    def test_disabled_integrations_are_not_discovered_or_imported(self):
        with patch("ev.plugins.entry_points") as discovery:
            self.assertEqual(load_enabled_plugins([], self.registry()), [])
            discovery.assert_not_called()

    def test_registration_preserves_existing_permission_gate(self):
        spec = self.spec()
        spec.permission = Permission.HIGH
        with patch("ev.plugins.entry_points", return_value=[self.point([spec])]):
            self.assertEqual(
                load_enabled_plugins(["fixture"], self.registry())[0]["state"], "REGISTERED"
            )
        self.assertTrue(spec.requires_confirmation)

    def test_invalid_later_declaration_never_partially_registers(self):
        registry = self.registry()
        spec = self.spec()
        bad = self.spec()
        bad.name = "system.override"
        with patch("ev.plugins.entry_points", return_value=[self.point([spec, bad])]):
            self.assertEqual(load_enabled_plugins(["fixture"], registry)[0]["state"], "FAILED")
        self.assertEqual(registry.catalog(), [])

    async def test_missing_required_capability_prevents_executor(self):
        spec = self.spec()
        spec.required_capabilities = ("DesktopControl",)
        with self.assertRaises(ValidationError):
            await self.registry().execute(spec, {})
        spec.executor.assert_not_called()

    async def test_result_contract_is_checked(self):
        spec = self.spec()
        spec.executor.return_value = {"ok": "invented"}
        with self.assertRaises(ValidationError):
            await self.registry().execute(spec, {})

    def test_unknown_or_duplicate_package_never_loads(self):
        point = self.point([self.spec()])
        with patch("ev.plugins.entry_points", return_value=[point, point]):
            self.assertEqual(
                load_enabled_plugins(["fixture"], self.registry())[0]["state"], "BLOCKED"
            )
        point.load.assert_not_called()

    def test_private_mode_rejects_network_capable_integrations(self):
        from ev.privacy import PrivacyPolicy

        spec = self.spec()
        spec.offline_available = False
        core = SimpleNamespace(
            config={"carlos": {"privacy_mode": "LOCAL ONLY"}},
            tools=SimpleNamespace(get=lambda name: spec),
        )
        policy = PrivacyPolicy(core)
        self.assertIn("disabled", policy.tool_error(spec.name))
        spec.offline_available = True
        self.assertEqual(policy.tool_error(spec.name), "")
