import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    "rollback_user", Path(__file__).parents[1] / "scripts/rollback-user.py"
)
rollback = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rollback)


class RollbackTests(unittest.TestCase):
    def test_restores_old_files_and_retains_new_without_consuming_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup = root / "backup"
            (backup / "files").mkdir(parents=True)
            target = root / "app"
            target.mkdir()
            (target / "version").write_text("new")
            previous = backup / "files/app"
            previous.mkdir()
            (previous / "version").write_text("old")
            (backup / "manifest.tsv").write_text(f"{target}\t{previous}\n")
            plan = rollback.read_plan(backup, {target})
            retained = rollback.restore(plan, backup)
            self.assertEqual((target / "version").read_text(), "old")
            self.assertEqual((previous / "version").read_text(), "old")
            self.assertEqual((retained / "0/version").read_text(), "new")

    def test_invalid_later_entry_is_rejected_before_any_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "files").mkdir()
            target = root / "target"
            target.write_text("preserve")
            (root / "manifest.tsv").write_text(f"{target}\t-\n/etc/passwd\t-\n")
            with self.assertRaises(ValueError):
                rollback.read_plan(root, {target})
            self.assertEqual(target.read_text(), "preserve")

    def test_missing_backup_is_not_treated_as_a_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.write_text("preserve")
            (root / "manifest.tsv").write_text(f"{target}\t{root}/files/missing\n")
            with self.assertRaises(ValueError):
                rollback.read_plan(root, {target})
            self.assertEqual(target.read_text(), "preserve")

    def test_failure_halfway_through_restore_retains_the_installed_version(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup = root / "backup"
            (backup / "files").mkdir(parents=True)
            targets = [root / "first", root / "second"]
            for i, target in enumerate(targets):
                target.write_text("new " + str(i))
                (backup / "files" / str(i)).write_text("old " + str(i))
            plan = [(target, backup / "files" / str(i)) for i, target in enumerate(targets)]
            original = Path.rename

            def fail_second(source, destination):
                if source.name == "1" and source.parent.name.startswith("restore-stage-"):
                    raise OSError("injected file replacement failure")
                return original(source, destination)

            with patch.object(Path, "rename", fail_second):
                with self.assertRaisesRegex(OSError, "injected"):
                    rollback.restore(plan, backup)
            self.assertEqual([p.read_text() for p in targets], ["new 0", "new 1"])
            self.assertEqual(
                [(backup / "files" / str(i)).read_text() for i in range(2)], ["old 0", "old 1"]
            )
