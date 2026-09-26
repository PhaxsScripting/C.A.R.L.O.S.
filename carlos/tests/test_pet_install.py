import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]


class PetInstallTests(unittest.TestCase):
    def test_pet_only_install_backs_up_files_without_enabling_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, home = root / "project", root / "home"
            for folder in ("scripts", "packaging", "build/ui"):
                (project / folder).mkdir(parents=True)
            for name in (
                "scripts/install-pet.sh",
                "scripts/carlos-pet",
                "packaging/carlos-pet.desktop.in",
            ):
                shutil.copy2(PROJECT / name, project / name)
            binary = project / "build/ui/ev-pet"
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)
            installed = home / ".local/share/ev/app/bin/ev-pet"
            installed.parent.mkdir(parents=True)
            installed.write_text("previous build")
            env = dict(
                os.environ,
                HOME=str(home),
                XDG_DATA_HOME=str(home / ".local/share"),
                XDG_STATE_HOME=str(home / ".local/state"),
                XDG_CONFIG_HOME=str(home / ".config"),
            )
            subprocess.run(
                ["sh", str(project / "scripts/install-pet.sh")],
                env=env,
                check=True,
                capture_output=True,
            )
            self.assertEqual(installed.read_text(), binary.read_text())
            manifests = list((home / ".local/state/ev/pet-backups").glob("*/manifest.json"))
            self.assertEqual(len(manifests), 1)
            saved = json.loads(manifests[0].read_text())
            entry = next(row for row in saved if row["target"] == str(installed))
            self.assertEqual(Path(entry["previous"]).read_text(), "previous build")
            self.assertTrue((home / ".local/share/applications/carlos-pet.desktop").exists())
            self.assertFalse((home / ".config/autostart").exists())
            self.assertFalse((home / ".local/bin/ev-core").exists())

    @unittest.skipUnless(os.uname().sysname == "Linux", "Linux installer requirement")
    def test_standard_install_refuses_to_omit_unbuilt_pet(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "scripts").mkdir()
            (project / "build/ui").mkdir(parents=True)
            shutil.copy2(PROJECT / "scripts/install-user.sh", project / "scripts/install-user.sh")
            ui = project / "build/ui/ev-ui"
            ui.write_text("#!/bin/sh\nexit 0\n")
            ui.chmod(0o755)
            result = subprocess.run(
                ["sh", str(project / "scripts/install-user.sh"), "--no-start"],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Carlos Pet has not been built", result.stderr)
