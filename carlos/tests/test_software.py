import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ev.tools.software import software_search, register_software_tools
from ev.tools.base import ValidationError


class SoftwareEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vdb = self.root / "vdb"
        self.repo = self.root / "repos"
        self.vdb.mkdir()
        self.repo.mkdir()
        for name, value in (("VDB_ROOT", self.vdb), ("REPOSITORY_ROOT", self.repo)):
            patcher = patch("ev.tools.software." + name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def installed(self, name="www-client/firefox-bin-154.0-r1"):
        path = self.vdb / name
        path.mkdir(parents=True)
        (path / "SLOT").write_text("rapid\n")
        (path / "repository").write_text("gentoo\n")
        return path

    def cached(self, name="www-client/firefox-bin-155.0"):
        path = self.repo / "gentoo/metadata/md5-cache" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "DESCRIPTION=Browser\nHOMEPAGE=https://example.com\nSLOT=rapid\nKEYWORDS=~amd64\nLICENSE=MPL-2.0\n"
        )
        return path

    def test_installed_and_cached_candidates_have_distinct_evidence(self):
        self.installed()
        self.cached()
        result = software_search({"query": "firefox", "scope": "both"}, None)
        self.assertFalse(result["partial"])
        installed, cached = result["packages"]
        self.assertEqual(installed["atom"], "www-client/firefox-bin")
        self.assertEqual(installed["version"], "154.0-r1")
        self.assertTrue(installed["recorded_installed"])
        self.assertFalse(cached["recorded_installed"])
        self.assertEqual(cached["description"], "Browser")
        self.assertIsNone(cached["installable"])
        self.assertIsNone(installed["runtime_working"])
        self.assertEqual(result["network_requests"], 0)
        self.assertFalse(result["changed"])

    def test_category_and_all_terms_match_without_guessing_software_names(self):
        self.installed("dev-tools/fictional-editor-2.0")
        self.installed("dev-tools/fictional-viewer-3.0")
        result = software_search({"query": "dev-tools fictional-editor"}, None)
        self.assertEqual([p["atom"] for p in result["packages"]], ["dev-tools/fictional-editor"])

    def test_result_limit_and_missing_cache_report_partial_not_absence(self):
        self.installed()
        self.installed("www-client/firefox-bin-153.0")
        result = software_search({"query": "firefox", "limit": 1}, None)
        self.assertTrue(result["partial"])
        self.assertEqual(result["count"], 1)
        absent = software_search({"query": "firefox", "scope": "repository"}, None)
        self.assertTrue(absent["partial"])
        self.assertEqual(absent["packages"], [])

    def test_symlink_and_fifo_metadata_are_not_followed_or_blocking(self):
        path = self.installed()
        (path / "SLOT").unlink()
        os.mkfifo(path / "SLOT")
        result = software_search({"query": "firefox"}, None)
        self.assertTrue(result["partial"])
        self.assertTrue(result["packages"][0]["metadata_incomplete"])
        (path / "SLOT").unlink()
        (path / "SLOT").symlink_to(path / "repository")
        self.assertTrue(software_search({"query": "firefox"}, None)["partial"])

    def test_symlinked_packages_and_categories_are_not_traversed(self):
        self.installed()
        (self.vdb / "www-client/fictional-1.0").symlink_to(
            self.vdb / "www-client/firefox-bin-154.0-r1", target_is_directory=True
        )
        (self.vdb / "fictional-category").symlink_to(
            self.vdb / "www-client", target_is_directory=True
        )
        self.assertEqual(software_search({"query": "fictional"}, None)["packages"], [])

    def test_query_rejects_shell_url_and_path_payloads_without_reading(self):
        for query in (
            ";echo hello",
            "https://example.com",
            "../../etc",
            "/etc/passwd",
            "$(id)",
            "x",
        ):
            with self.subTest(query=query), patch("ev.tools.software._entries") as scan:
                with self.assertRaises(ValidationError):
                    software_search({"query": query}, None)
                scan.assert_not_called()

    def test_tool_registered_readonly_without_install_authority(self):
        class Registry:
            def register(self, spec):
                self.spec = spec

        registry = Registry()
        register_software_tools(registry)
        self.assertTrue(registry.spec.read_only)
        self.assertEqual(registry.spec.name, "software.search")
        self.assertFalse(registry.spec.requires_confirmation)
