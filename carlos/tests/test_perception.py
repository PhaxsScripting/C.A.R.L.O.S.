from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "core"))

from ev.accessibility import AccessibilityBridge  # noqa: E402
from ev.vision import ScreenPerception  # noqa: E402


class FakeDesktop:
    def __init__(self) -> None:
        self.forced = False
        self.active_id = "{other-window}"
        self.activations: list[str] = []
        self.bridge = self

    async def request(self, action: str, arguments: dict):
        if action == "activate":
            self.active_id = str(arguments["window_id"])
            self.activations.append(self.active_id)
        return {"ok": True}

    async def snapshot(self, force: bool = False):
        self.forced = force
        return {
            "active_window_id": self.active_id,
            "windows": [
                {"id": "{window-test}", "geometry": {"x": 12, "y": 34, "width": 80, "height": 60}},
                {"id": "{other-window}", "geometry": {"x": 2, "y": 4, "width": 40, "height": 30}},
            ],
            "outputs": [
                {
                    "name": "HDMI-A-1",
                    "enabled": True,
                    "geometry": {"x": 0, "y": 0, "width": 100, "height": 70},
                }
            ],
        }


class FakeCaptureProcess:
    returncode = 0

    def __init__(self, path: Path, size: tuple[int, int]) -> None:
        self.path = path
        self.size = size

    async def communicate(self):
        Image.new("RGB", self.size, "navy").save(self.path, "PNG")
        return b"", b""


class FakeOcrCompleted:
    returncode = 0
    stdout = 'EV_OCR_JSON:{"engine":"test","elements":[{"text":"E.V.","confidence":0.99,"box":[],"center":{"x":1,"y":2}}],"text":"E.V.","count":1,"duration_ms":12.5}\n'
    stderr = ""


class FakeAccessible:
    def __init__(
        self, name: str, role: str, path: str, children=None, pid: int = 0, actions=None
    ) -> None:
        self._name = name
        self._role = role
        self.path = path
        self._children = list(children or [])
        self._pid = pid
        self._actions = list(actions or [])

    def get_name(self):
        return self._name

    def get_description(self):
        return ""

    def get_role_name(self):
        return self._role

    def get_process_id(self):
        return self._pid

    def get_child_count(self):
        return len(self._children)

    def get_child_at_index(self, index):
        return self._children[index]

    def get_n_actions(self):
        return len(self._actions)

    def get_action_name(self, index):
        return self._actions[index]

    def do_action(self, index):
        return 0 <= index < len(self._actions)

    def is_editable_text(self):
        return False

    def is_action(self):
        return bool(self._actions)

    def is_value(self):
        return False


class FakeEditable(FakeAccessible):
    def __init__(self, name: str, path: str) -> None:
        super().__init__(name, "text", path)
        self.value = ""

    def is_editable_text(self):
        return True

    def set_text_contents(self, value):
        self.value = value
        return True


class PerceptionTests(unittest.IsolatedAsyncioTestCase):
    def test_ocr_installation_does_not_claim_visual_reasoning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "venv/bin/python"
            executable.parent.mkdir(parents=True)
            executable.touch()
            (root / "venv/lib/python3.14/site-packages/rapidocr").mkdir(parents=True)
            perception = ScreenPerception(
                root / "captures", FakeDesktop(), {"ocr_python": str(executable)}
            )
            status = perception.status()
            self.assertTrue(status["ocr"])
            self.assertFalse(status["semantic_understanding"])
            self.assertEqual(status["visual_reasoning"], "not_configured")

    def test_deep_web_controls_are_reachable_and_cycles_are_bounded(self):
        target = FakeAccessible("Full screen (f)", "button", "/target", actions=["press"])
        root = target
        for index in range(16):
            root = FakeAccessible("", "section", f"/wrapper/{index}", [root])
        app = FakeAccessible("Firefox", "application", "/app", [root])
        # A cycle must not make either search or activation loop forever.
        target._children.append(root)
        bridge = AccessibilityBridge()
        bridge.Atspi = object()
        with (
            patch.object(bridge, "_applications", return_value=[app]),
            patch.object(bridge, "status", return_value={"available": True}),
        ):
            listing = bridge.list_elements("Firefox", "screen", "button")
            self.assertEqual(len(listing["elements"]), 1)
            self.assertLess(listing["scanned"], 25)
            self.assertTrue(bridge.activate("Firefox", "Full screen (f)", "button")["verified"])

    def test_expired_private_capture_is_pruned_on_startup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / f"{'a' * 32}.png"
            Image.new("RGB", (8, 8), "navy").save(path)
            old = time.time() - 700
            path.touch()
            import os

            os.utime(path, (old, old))
            ScreenPerception(Path(temporary), FakeDesktop())
            self.assertFalse(path.exists())

    async def test_spectacle_exact_window_is_activated_then_prior_focus_is_restored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            desktop = FakeDesktop()
            perception = ScreenPerception(Path(temporary), desktop)

            async def spawn(*arguments, **_kwargs):
                self.assertIn("--activewindow", arguments)
                self.assertIn("--no-shadow", arguments)
                return FakeCaptureProcess(Path(arguments[-1]), (80, 60))

            with (
                patch.object(
                    ScreenPerception, "_backend", return_value=("spectacle", "/usr/bin/spectacle")
                ),
                patch("ev.vision.asyncio.create_subprocess_exec", side_effect=spawn),
            ):
                result = await perception.capture(window_id="{window-test}")
            self.assertTrue(result["verified"])
            self.assertTrue(result["focus_restored"])
            self.assertEqual(desktop.activations, ["{window-test}", "{other-window}"])
            perception.delete(result["capture_id"])

    async def test_exact_window_capture_forces_refresh_and_verifies_private_png(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            desktop = FakeDesktop()
            perception = ScreenPerception(Path(temporary), desktop)

            async def spawn(*arguments, **_kwargs):
                self.assertIn("12,34 80x60", arguments)
                return FakeCaptureProcess(Path(arguments[-1]), (80, 60))

            with (
                patch.object(ScreenPerception, "_backend", return_value=("grim", "/usr/bin/grim")),
                patch("ev.vision.asyncio.create_subprocess_exec", side_effect=spawn),
            ):
                result = await perception.capture(window_id="{window-test}")
            self.assertTrue(desktop.forced)
            self.assertTrue(result["verified"])
            self.assertEqual((result["width"], result["height"]), (80, 60))
            self.assertEqual(Path(result["path"]).stat().st_mode & 0o777, 0o600)
            removed = perception.delete(result["capture_id"])
            self.assertTrue(removed["verified"])
            self.assertTrue(removed["removed"])

    async def test_dimension_mismatch_removes_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            perception = ScreenPerception(Path(temporary), FakeDesktop())

            async def spawn(*arguments, **_kwargs):
                return FakeCaptureProcess(Path(arguments[-1]), (79, 60))

            with (
                patch.object(ScreenPerception, "_backend", return_value=("grim", "/usr/bin/grim")),
                patch("ev.vision.asyncio.create_subprocess_exec", side_effect=spawn),
            ):
                with self.assertRaisesRegex(RuntimeError, "dimensions"):
                    await perception.capture(window_id="{window-test}")
            self.assertEqual(list(Path(temporary).glob("*.png")), [])

    def test_accessibility_word_matching_is_case_and_punctuation_insensitive(self) -> None:
        self.assertEqual(
            AccessibilityBridge._words("E.V. Control-Center"), {"e", "v", "control", "center"}
        )

    def test_accessibility_action_is_scoped_to_exact_pid_and_window_title(self) -> None:
        save = FakeAccessible("Save", "push button", "/app/frame/save", actions=["click"])
        frame = FakeAccessible("Project - Firefox", "frame", "/app/frame", [save])
        other = FakeAccessible(
            "Other - Firefox",
            "frame",
            "/app/other",
            [
                FakeAccessible("Save", "push button", "/app/other/save", actions=["click"]),
            ],
        )
        app = FakeAccessible("Firefox", "application", "/app", [frame, other], pid=1234)
        desktop = FakeAccessible("Desktop", "desktop", "/desktop", [app])

        class FakeAtspi:
            @staticmethod
            def get_desktop(_index):
                return desktop

        bridge = AccessibilityBridge()
        bridge.Atspi = FakeAtspi
        result = bridge.activate("Firefox", "Save", "button", 1234, "Project - Firefox")
        self.assertTrue(result["action_accepted"])
        current = bridge.activate("current window", "Save", "button", 1234, "Project - Firefox")
        self.assertTrue(current["action_accepted"])
        with self.assertRaisesRegex(RuntimeError, "missing"):
            bridge.activate("Firefox", "Save", "button", 9999, "Project - Firefox")
        with self.assertRaisesRegex(RuntimeError, "missing"):
            bridge.activate("Firefox", "Save", "button", 1234, "Unknown - Firefox")

    def test_accessibility_text_write_requires_real_readback(self) -> None:
        editor = FakeEditable("Search", "/app/frame/search")
        frame = FakeAccessible("Project - Firefox", "frame", "/app/frame", [editor])
        app = FakeAccessible("Firefox", "application", "/app", [frame], pid=1234)
        desktop = FakeAccessible("Desktop", "desktop", "/desktop", [app])

        class FakeText:
            @staticmethod
            def get_text(node, start, end):
                self.assertEqual((start, end), (0, -1))
                return node.value

        class FakeAtspi:
            Text = FakeText

            @staticmethod
            def get_desktop(_index):
                return desktop

        bridge = AccessibilityBridge()
        bridge.Atspi = FakeAtspi
        result = bridge.set_text("Firefox", "Search", "hello", "text", 1234, "Project - Firefox")
        self.assertTrue(result["verified"])
        self.assertTrue(result["readback_available"])
        self.assertFalse(result["text_returned"])

    def test_accessibility_enable_sets_and_verifies_both_session_flags(self) -> None:
        bridge = AccessibilityBridge()
        bridge.Atspi = object()
        completed = subprocess.CompletedProcess([], 0, "()\n", "")
        with (
            patch("ev.accessibility.subprocess.run", return_value=completed) as run,
            patch.object(AccessibilityBridge, "_status_flag", return_value=True) as flag,
        ):
            result = bridge.enable_session(True)
        self.assertTrue(result["verified"])
        self.assertEqual(run.call_count, 2)
        self.assertEqual(
            {call.args[0][-2] for call in run.call_args_list},
            {"IsEnabled", "ScreenReaderEnabled"},
        )
        self.assertEqual(flag.call_count, 2)

    def test_accessibility_effective_state_requires_both_flags(self) -> None:
        with patch.object(AccessibilityBridge, "_status_flag", side_effect=[True, False]):
            self.assertFalse(AccessibilityBridge._enabled())

    def test_ocr_accepts_only_existing_private_capture_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            capture_id = "a" * 32
            Image.new("RGB", (80, 60), "navy").save(root / f"{capture_id}.png")
            perception = ScreenPerception(root, FakeDesktop(), {"ocr_python": sys.executable})
            with (
                patch.object(ScreenPerception, "status", return_value={"ocr": True}),
                patch("ev.vision.subprocess.run", return_value=FakeOcrCompleted()),
            ):
                result = perception.ocr(capture_id)
            self.assertTrue(result["verified"])
            self.assertEqual(result["text"], "E.V.")
            self.assertTrue((root / f"{capture_id}.png").exists())
            with self.assertRaisesRegex(ValueError, "invalid capture id"):
                perception.ocr("../escape")


if __name__ == "__main__":
    unittest.main()
