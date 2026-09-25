import copy
import json
import unittest

from ev.ai.tool_context import encode_tool_result


class ToolContextTests(unittest.TestCase):
    def test_small_evidence_is_unchanged(self):
        result = {"status": "completed", "result": {"window_id": "exact-id", "verified": True}}
        self.assertEqual(json.loads(encode_tool_result(result)), result)

    def test_large_observation_is_valid_json_and_never_an_absence_claim(self):
        result = {
            "status": "completed",
            "tool": "desktop.observe",
            "result": {
                "active_window_id": "exact-active-window",
                "captured_at_monotonic": 100.0,
                "windows": [
                    {"id": str(i), "title": "large-window-title" * 100} for i in range(100)
                ],
            },
            "execution": {"verified": True, "scope": "observation_only"},
        }
        before = copy.deepcopy(result)
        encoded = encode_tool_result(result)
        self.assertLessEqual(len(encoded), 6000)
        summary = json.loads(encoded)
        self.assertTrue(summary["context_truncated"])
        self.assertEqual(summary["result_summary"]["active_window_id"], "exact-active-window")
        self.assertIn("windows", summary["omitted_fields"])
        self.assertNotIn("windows", summary["result_summary"])
        self.assertIn("not observed absent", summary["context_note"])
        self.assertEqual(result, before)

    def test_long_targets_are_omitted_instead_of_cut_into_different_targets(self):
        path = "/target/" + "x" * 8000
        summary = json.loads(
            encode_tool_result({"status": "completed", "result": {"path": path}}, 2000)
        )
        self.assertNotIn("path", summary["result_summary"])
        self.assertNotIn(path[:100], json.dumps(summary))

    def test_failed_large_tool_retains_failure_and_does_not_promote_it(self):
        result = {
            "status": "failed",
            "error": "Postcondition not met",
            "result": {"log": "x" * 20000},
            "execution": {
                "ok": False,
                "status": "FAILED",
                "verified": False,
                "scope": "tool_effect",
            },
        }
        summary = json.loads(encode_tool_result(result))
        self.assertEqual(summary["status"], "failed")
        self.assertFalse(summary["reported_execution"]["verified"])

    def test_metadata_cannot_overflow_small_result_budget(self):
        result = {key: "x" * 256 for key in ("status", "tool", "correlation_id", "error")}
        result.update(
            result={"large": "x" * 20000},
            execution={
                key: "x" * 90 for key in ("ok", "status", "verified", "scope", "changed_state")
            },
        )
        encoded = encode_tool_result(result, 2000)
        self.assertLessEqual(len(encoded), 2000)
        self.assertTrue(json.loads(encoded)["context_truncated"])
