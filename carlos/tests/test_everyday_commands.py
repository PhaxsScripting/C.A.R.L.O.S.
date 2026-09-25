import logging
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from ev.commands import direct_action, normalize_web_url
from ev.ai import LocalHybridProvider
from ev.tools.browser import open_url
from ev.tools import ToolContext, ToolRegistry, register_builtin_tools
from ev.tools.daily import register_daily_tools
from ev.events import PhaxEventBus
from ev.state import StateMachine
from ev.planner import TaskPlanner
from ev.tools import builtin


class EverydayGrammarTests(unittest.IsolatedAsyncioTestCase):
    def test_natural_url_requests_preserve_url_and_browser(self):
        for text, url in (
            ("open youtube.com in Firefox", "https://youtube.com"),
            ("go to youtube in Firefox", "https://youtube.com"),
            ("open github dot com for me in Firefox", "https://github.com"),
            ("open Firefox and go to github.com", "https://github.com"),
            ("open Firefox to youtube.com", "https://youtube.com"),
            (
                "visit https://example.com/Path?q=A%2BB&x=2#Somewhere in Firefox",
                "https://example.com/Path?q=A%2BB&x=2#Somewhere",
            ),
            ("open https://example.com/Path?q=hi!", "https://example.com/Path?q=hi!"),
            (
                "search for C++ examples in Firefox",
                "https://www.google.com/search?q=C%2B%2B+examples",
            ),
        ):
            with self.subTest(text=text):
                action = direct_action(text)
                self.assertEqual(action.tool, "browser.open_url")
                self.assertEqual(action.arguments["url"], url)

    def test_browser_urls_reject_code_credentials_and_argument_injection(self):
        for value in (
            "javascript:alert(1)",
            "file:///etc/passwd",
            "data:text/html,hello",
            "https://user:secret@example.com",
            "https://example.com\n--profile /tmp/x",
            "--new-instance",
            "https://example.com:99999",
            "https://example.com\\@other.com",
        ):
            with self.subTest(value=value), self.assertRaises((ValueError, UnicodeError)):
                normalize_web_url(value)

    def test_volume_matrix_understands_numbers_and_relative_vs_absolute(self):
        examples = {
            "set volume to 40 percent": ("audio.set_volume", {"percent": 40}),
            "turn the volume down to twenty five percent": ("audio.set_volume", {"percent": 25}),
            "volume fifty": ("audio.set_volume", {"percent": 50}),
            "set my volume to half": ("audio.set_volume", {"percent": 50}),
            "lower the volume by five": ("audio.adjust_volume", {"delta": -5}),
            "increase volume by 15 percent": ("audio.adjust_volume", {"delta": 15}),
            "volume down": ("audio.adjust_volume", {"delta": -10}),
            "turn volume up five": ("audio.adjust_volume", {"delta": 5}),
            "lower it by five": ("audio.adjust_volume", {"delta": -5}),
            "turn it up": ("audio.adjust_volume", {"delta": 10}),
            "turn down the volume": ("audio.adjust_volume", {"delta": -10}),
            "make it quieter": ("audio.adjust_volume", {"delta": -10}),
            "mute": ("audio.set_mute", {"muted": True}),
            "unmute my sound": ("audio.set_mute", {"muted": False}),
            "what's my volume": ("audio.get_volume", {}),
        }
        for text, expected in examples.items():
            for prefix in ("", "please ", "can you ", "could you please "):
                with self.subTest(text=prefix + text):
                    action = direct_action(prefix + text)
                    self.assertEqual((action.tool, action.arguments), expected)

    async def test_volume_read_does_not_turn_into_change_due_to_context(self):
        provider = LocalHybridProvider({})
        provider.local.begin = AsyncMock(side_effect=AssertionError("Everyday route invoked model"))
        turn = await provider.begin(
            "what's my volume", [{"role": "user", "content": "turn it up"}], [], []
        )
        answer = await provider.continue_with_tools(
            turn, [(turn.tool_calls[0], {"result": {"percent": 45, "muted": False}})], []
        )
        self.assertFalse(answer.tool_calls)
        self.assertIn("45%", answer.text)

    async def test_unsupported_target_or_number_does_not_change_global_audio(self):
        provider = LocalHybridProvider({})
        for text in (
            "set volume to 150",
            "set Firefox volume to 20",
            "unmute Firefox",
            "set my microphone volume to 30",
        ):
            result = await provider.offline.begin(text, [], [], [])
            self.assertFalse(result.tool_calls, text)

    async def test_registered_async_spotify_diagnostic_is_awaited(self):
        bus = PhaxEventBus()
        spotify = SimpleNamespace(diagnose=AsyncMock(return_value={"available": False}))
        context = ToolContext(
            {"security": {"max_tool_output_bytes": 10000}},
            bus,
            logging.getLogger("fixture"),
            spotify=spotify,
        )
        registry = ToolRegistry(context)
        register_daily_tools(registry)
        result = await registry.execute(registry.get("spotify.diagnose"), {})
        self.assertFalse(result["available"])
        spotify.diagnose.assert_awaited_once()

    def test_discussion_negation_and_typing_do_not_become_daily_actions(self):
        for text in (
            "don't open youtube.com in Firefox",
            "how do I open youtube.com in Firefox?",
            'type "set volume to 50"',
            "explain how to mute",
            "I like volume up buttons",
            "Can you explain how to play my Coding playlist?",
        ):
            self.assertIsNone(direct_action(text), text)

    def test_playlist_request_uses_saved_playlist_not_global_track_search(self):
        for text in (
            "Play a random song on my coding playlist on Spotify.",
            "play any song from my Coding playlist on Spotify",
            "shuffle my Coding playlist",
            "play my playlist Coding on Spotify",
        ):
            action = direct_action(text)
            self.assertEqual(action.tool, "spotify.play")
            self.assertEqual(action.arguments["query"].lower(), "coding")
            self.assertTrue(action.arguments["own_playlist"])
            self.assertEqual(action.arguments["kind"], "playlist")

    def test_plans_validate_registered_schemas_without_executing_host_actions(self):
        bus = PhaxEventBus()
        registry = ToolRegistry(ToolContext({}, bus, logging.getLogger("everyday-fixture")))
        register_builtin_tools(registry)
        register_daily_tools(registry)
        planner = TaskPlanner(registry, AsyncMock(), bus, StateMachine(bus))
        for text in (
            "open github.com in Firefox",
            "set volume to fifty",
            "lower volume by five",
            "play a random song on my coding playlist on Spotify",
            "check Spotify",
            "set a five minute timer",
            "minimize my window",
            "shut down my computer",
        ):
            plan = planner.try_plan(text, "fixture")
            self.assertIsNotNone(plan, text)
            for step in plan.steps:
                self.assertNotEqual(step.permission_class, "UNKNOWN", step.tool)
        planner.request_tool.assert_not_awaited()


class BrowserHandoffTests(unittest.TestCase):
    def test_launch_includes_one_literal_url_and_no_empty_launch_or_shell(self):
        with patch("ev.tools.browser.desktop_entries", return_value={"firefox-bin": {}}), patch(
            "ev.tools.browser.subprocess.run", return_value=SimpleNamespace(returncode=0)
        ) as run:
            result = open_url(
                {"url": "https://example.com/?q=one&two=3", "browser": "firefox"}, None
            )
        self.assertEqual(
            run.call_args.args[0],
            ["/usr/bin/gtk-launch", "firefox-bin", "https://example.com/?q=one&two=3"],
        )
        self.assertEqual(run.call_count, 1)
        self.assertTrue(result["requested"])
        self.assertFalse(result["verified"])
        self.assertFalse(run.call_args.kwargs.get("shell", False))

    def test_launch_timeout_is_uncertain_not_repeated(self):
        with patch("ev.tools.browser.desktop_entries", return_value={"firefox-bin": {}}), patch(
            "ev.tools.browser.subprocess.run",
            side_effect=subprocess.TimeoutExpired("gtk-launch", 3),
        ) as run:
            result = open_url({"url": "example.com", "browser": "firefox"}, None)
        self.assertTrue(result["uncertain"])
        self.assertFalse(result["requested"])
        self.assertEqual(run.call_count, 1)


class VerifiedAudioTests(unittest.TestCase):
    def setUp(self):
        self.percent = 45
        self.muted = False
        self.writes = []
        self.accept = True

        def run(args, **kwargs):
            if args[1] == "get-default-sink":
                return {"ok": True, "stdout": "exact-speaker\n"}
            self.writes.append(args)
            self.assertEqual(args[2], "exact-speaker")
            if self.accept:
                if args[1] == "set-sink-volume":
                    self.percent = int(args[3].rstrip("%"))
                else:
                    self.muted = args[3] == "1"
            return {"ok": True, "stdout": "", "stderr": ""}

        def listing(args):
            return [
                {
                    "name": "exact-speaker",
                    "mute": self.muted,
                    "volume": {"mono": {"value_percent": f"{self.percent}%"}},
                }
            ]

        self.patch_run = patch("ev.tools.builtin.run_command", side_effect=run)
        self.patch_json = patch("ev.tools.builtin._pactl_json", side_effect=listing)
        self.patch_run.start()
        self.patch_json.start()
        self.addCleanup(self.patch_run.stop)
        self.addCleanup(self.patch_json.stop)

    def test_relative_volume_and_mute_are_read_back(self):
        self.assertEqual(builtin.adjust_volume({"delta": -5}, None)["percent"], 40)
        self.assertTrue(builtin.set_volume({"percent": 30}, None)["verified"])
        self.assertTrue(builtin.set_mute({"muted": True}, None)["verified"])
        self.assertTrue(builtin.get_volume({}, None)["muted"])

    def test_pactl_acceptance_without_actual_change_is_not_success(self):
        self.accept = False
        self.assertFalse(builtin.set_volume({"percent": 30}, None)["set"])
        self.assertFalse(builtin.set_mute({"muted": True}, None)["set"])

    def test_disconnected_output_is_not_replaced_with_new_default(self):
        with patch("ev.tools.builtin._pactl_json", return_value=[]):
            with self.assertRaises(RuntimeError):
                builtin.set_volume({"percent": 30}, None)
        self.assertFalse(self.writes)
