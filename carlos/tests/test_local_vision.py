import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image
from ev.ai.local_vision import LocalVisualReasoner, prepare_image
from ev.ai.local_llama import LocalLlamaProvider
from ev.ai.base import ProviderError
from ev.paths import Paths
from ev.service import CarlosCore
from ev.tools.results import evaluate_result


class LocalVisionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "fixture.png"
        Image.new("RGB", (1200, 600), "red").save(self.path)
        self.path.chmod(0o600)
        self.reasoner = LocalVisualReasoner({}, self.root / "owner.json")

    def test_private_image_is_bounded_and_encoded_only_in_memory(self):
        result = prepare_image(self.path)
        self.assertEqual(result["original_size"], (1200, 600))
        self.assertEqual(result["input_size"], (768, 384))
        self.assertTrue(result["url"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_links_public_files_and_non_png_are_rejected(self):
        link = self.root / "link.png"
        link.symlink_to(self.path)
        with self.assertRaises(OSError):
            prepare_image(link)
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            prepare_image(self.path)
        self.path.chmod(0o600)
        hard = self.root / "hard.png"
        os.link(self.path, hard)
        with self.assertRaises(ValueError):
            prepare_image(self.path)

    def test_projector_is_explicit_and_cpu_bound(self):
        command = LocalLlamaProvider({"mmproj_path": "/exact/projector.gguf"})._server_command()
        self.assertIn("--mmproj", command)
        self.assertEqual(command[command.index("--mmproj-device") + 1], "none")
        self.assertEqual(command[command.index("--image-max-tokens") + 1], "512")

    async def test_disabled_model_does_not_spawn(self):
        with patch("ev.ai.local_vision.LocalLlamaProvider._post", new_callable=AsyncMock) as post:
            with self.assertRaises(ProviderError):
                await self.reasoner.describe(self.path, "what is here?")
            post.assert_not_awaited()

    async def test_local_request_has_no_tools_and_always_closes_runtime(self):
        response = {"choices": [{"message": {"content": "A red rectangle."}}]}
        with patch.object(self.reasoner, "status", return_value={"available": True}), patch(
            "ev.ai.local_vision.psutil.virtual_memory",
            return_value=SimpleNamespace(available=8 * 1024**3),
        ), patch.object(
            LocalLlamaProvider, "_post", new=AsyncMock(return_value=response)
        ) as post, patch.object(
            LocalLlamaProvider, "close", new_callable=AsyncMock
        ) as close:
            result = await self.reasoner.describe(self.path, "What shape?")
            self.assertEqual(post.await_args.args[1], [])
            self.assertIn("untrusted", post.await_args.args[0][0]["content"])
            self.assertTrue(result["local_only"])
            self.assertFalse(result["scene_accuracy_verified"])
            self.assertFalse(result["coordinate_actions_allowed"])
            close.assert_awaited_once()

    async def test_cancellation_closes_runtime_and_releases_lock(self):
        with patch.object(self.reasoner, "status", return_value={"available": True}), patch(
            "ev.ai.local_vision.psutil.virtual_memory",
            return_value=SimpleNamespace(available=8 * 1024**3),
        ), patch.object(
            LocalLlamaProvider, "_post", new=AsyncMock(side_effect=asyncio.CancelledError)
        ), patch.object(
            LocalLlamaProvider, "close", new_callable=AsyncMock
        ) as close:
            with self.assertRaises(asyncio.CancelledError):
                await self.reasoner.describe(self.path, "What shape?")
            close.assert_awaited_once()
            self.assertFalse(self.reasoner._lock.locked())

    async def test_low_ram_refuses_model_start(self):
        with patch.object(self.reasoner, "status", return_value={"available": True}), patch(
            "ev.ai.local_vision.psutil.virtual_memory", return_value=SimpleNamespace(available=1024)
        ), patch.object(LocalLlamaProvider, "_post", new_callable=AsyncMock) as post:
            with self.assertRaisesRegex(ProviderError, "RAM"):
                await self.reasoner.describe(self.path, "What shape?")
            post.assert_not_awaited()

    async def test_busy_reasoner_does_not_queue_another_model(self):
        await self.reasoner._lock.acquire()
        try:
            with patch.object(
                self.reasoner, "status", return_value={"available": True}
            ), patch.object(LocalLlamaProvider, "_post", new_callable=AsyncMock) as post:
                with self.assertRaisesRegex(ProviderError, "busy"):
                    await self.reasoner.describe(self.path, "What shape?")
                post.assert_not_awaited()
        finally:
            self.reasoner._lock.release()

    async def test_returned_tool_call_is_not_treated_as_description(self):
        response = {
            "choices": [{"message": {"content": "Done", "tool_calls": [{"name": "click"}]}}]
        }
        with patch.object(self.reasoner, "status", return_value={"available": True}), patch(
            "ev.ai.local_vision.psutil.virtual_memory",
            return_value=SimpleNamespace(available=8 * 1024**3),
        ), patch.object(
            LocalLlamaProvider, "_post", new=AsyncMock(return_value=response)
        ), patch.object(
            LocalLlamaProvider, "close", new_callable=AsyncMock
        ) as close:
            with self.assertRaisesRegex(ProviderError, "usable description"):
                await self.reasoner.describe(self.path, "What shape?")
            close.assert_awaited_once()

    async def test_capture_id_not_an_arbitrary_model_file_path(self):
        from ev.vision import ScreenPerception

        perception = ScreenPerception(self.root / "captures", object(), {})
        perception.reasoner.describe = AsyncMock()
        for ident in ("../fixture", "../" + "a" * 32, "a" * 32):
            with self.assertRaises(ValueError):
                await perception.describe(ident, "What shape?")
        perception.reasoner.describe.assert_not_awaited()

    async def test_cloud_active_brain_cannot_receive_private_visual_descriptions(self):
        service = CarlosCore(
            paths=Paths(*(self.root / n for n in ("config", "data", "state", "cache", "runtime")))
        )
        self.addCleanup(service.memory.close)
        service.config["providers"]["active"] = "nvidia"
        service.vision.describe = AsyncMock()
        spec = service.tools.get("vision.describe")
        with self.assertRaisesRegex(ValueError, "cloud tool-result upload is blocked"):
            await spec.executor(
                {"capture_id": "a" * 32, "question": "What shape?"}, service.tools.context
            )
        service.vision.describe.assert_not_awaited()

    def test_visual_inference_is_never_classified_as_verified_scene_truth(self):
        result = evaluate_result(
            "vision.describe", {"description": "A button.", "verified": True}, read_only=True
        )
        self.assertTrue(result.ok)
        self.assertFalse(result.verified)
        self.assertEqual(result.scope, "visual_inference")

    async def test_window_inference_marks_identity_changes_and_cleans_capture(self):
        from ev.vision import ScreenPerception

        window = {
            "id": "window",
            "pid": 123,
            "title": "Fixture",
            "app_id": "fixture",
            "geometry": {"x": 0, "y": 0, "width": 512, "height": 256},
        }
        desktop = SimpleNamespace(
            snapshot=AsyncMock(
                side_effect=[{"windows": [window]}, {"windows": [{**window, "pid": 456}]}]
            )
        )
        perception = ScreenPerception(self.root / "captures", desktop)
        perception.reasoner.status = lambda: {"available": True}
        ident = "a" * 32
        capture = perception.capture_root / (ident + ".png")
        capture.touch()
        perception.capture = AsyncMock(return_value={"capture_id": ident, "focus_restored": True})
        perception.describe = AsyncMock(
            return_value={"description": "A fixture", "coordinate_actions_allowed": False}
        )
        result = await perception.inspect_window("window", "What is here?")
        self.assertTrue(result["stale"])
        self.assertFalse(result["target_identity_unchanged"])
        self.assertFalse(capture.exists())
        perception.capture.assert_awaited_once_with(window_id="window")

    async def test_window_cancellation_deletes_capture(self):
        from ev.vision import ScreenPerception

        desktop = SimpleNamespace(
            snapshot=AsyncMock(return_value={"windows": [{"id": "window", "pid": 123}]})
        )
        perception = ScreenPerception(self.root / "captures", desktop)
        perception.reasoner.status = lambda: {"available": True}
        ident = "a" * 32
        capture = perception.capture_root / (ident + ".png")
        capture.touch()
        perception.capture = AsyncMock(return_value={"capture_id": ident})
        perception.describe = AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await perception.inspect_window("window", "What is here?")
        self.assertFalse(capture.exists())

    async def test_capture_cancellation_stops_exact_backend_child(self):
        from ev.vision import ScreenPerception

        perception = ScreenPerception(self.root / "captures", object())
        process = Mock(
            returncode=None,
            communicate=AsyncMock(side_effect=asyncio.CancelledError),
            wait=AsyncMock(return_value=0),
        )
        with patch.object(perception, "status", return_value={"available": True}), patch.object(
            perception, "_backend", return_value=("grim", "/test/grim")
        ), patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)):
            with self.assertRaises(asyncio.CancelledError):
                await perception.capture()
        process.terminate.assert_called_once()
        process.wait.assert_awaited_once()
        self.assertEqual(list(perception.capture_root.glob("*.png")), [])

    async def test_delayed_focus_settles_but_focus_change_during_capture_is_rejected(self):
        from ev.vision import ScreenPerception

        window = {
            "id": "window",
            "pid": 123,
            "title": "Fixture",
            "geometry": {"x": 0, "y": 0, "width": 80, "height": 60},
        }
        for changed in (False, True):
            worlds = [
                {"windows": [window], "active_window_id": ""},
                {"windows": [window], "active_window_id": ""},
                {"windows": [window], "active_window_id": "window"},
                {"windows": [window], "active_window_id": "other" if changed else "window"},
            ]
            desktop = SimpleNamespace(
                snapshot=AsyncMock(side_effect=worlds), bridge=SimpleNamespace(request=AsyncMock())
            )
            perception = ScreenPerception(self.root / str(changed), desktop)

            async def spawn(*args, **kwargs):
                Image.new("RGB", (80, 60)).save(args[args.index("--output") + 1])
                return Mock(returncode=0, communicate=AsyncMock(return_value=(b"", b"")))

            with patch.object(perception, "status", return_value={"available": True}), patch.object(
                perception, "_backend", return_value=("spectacle", "/test/spectacle")
            ), patch("asyncio.create_subprocess_exec", side_effect=spawn):
                if changed:
                    with self.assertRaisesRegex(RuntimeError, "discarded uncertain"):
                        await perception.capture(window_id="window")
                    self.assertEqual(list(perception.capture_root.glob("*.png")), [])
                else:
                    result = await perception.capture(window_id="window")
                    self.assertTrue(result["captured"])
                    perception.delete(result["capture_id"])
