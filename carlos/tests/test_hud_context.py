import unittest
from ev.hud_context import PhaxReference


class FailureReferenceTests(unittest.TestCase):
    def test_displayed_failure_expires_without_refreshing_on_redisplay(self):
        ref = PhaxReference()
        card = {
            "proposal_id": "one",
            "state": "FAILED",
            "observed_monotonic": 100,
            "project": "/fixture",
            "message": "Failure",
        }
        ref.remember(card, "one", 110)
        self.assertEqual(ref.get(115)["project"], "/fixture")
        ref.remember(card, "one", 210)
        with self.assertRaises(ValueError):
            ref.get(221)

    def test_unknown_or_successful_cards_cannot_become_repair_authority(self):
        ref = PhaxReference()
        for card in [{}, {"proposal_id": "one", "state": "ACTIVE", "observed_monotonic": 100}]:
            with self.assertRaises(ValueError):
                ref.remember(card, "one", 105)
        with self.assertRaises(ValueError):
            ref.get(110)


class ContextRepairTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_failure_creates_only_a_reviewable_proposal(self):
        import time
        from unittest.mock import AsyncMock
        from ev.service import CarlosCore

        service = CarlosCore.__new__(CarlosCore)
        service.failure_reference = PhaxReference()
        service.failure_reference.remember(
            {
                "proposal_id": "job",
                "state": "FAILED",
                "project": "/fixture",
                "message": "Untrusted failure diagnostics",
                "observed_monotonic": time.monotonic(),
            },
            "job",
        )
        service._request_model_tool = AsyncMock(
            return_value={"status": "completed", "result": {"status": "READY_FOR_REVIEW"}}
        )
        result = await service._submit_action_clauses_impl("Have Codex fix that.", "reference-test")
        call = service._request_model_tool.await_args.args[0]
        self.assertEqual(call["name"], "development.coding_agent_propose")
        self.assertEqual(call["arguments"]["project"], "/fixture")
        self.assertIn("untrusted", call["arguments"]["request"])
        self.assertIn("No repair has run", result["response"])

    async def test_expired_reference_never_selects_a_project_or_starts_a_job(self):
        from unittest.mock import AsyncMock
        from ev.service import CarlosCore

        service = CarlosCore.__new__(CarlosCore)
        service.failure_reference = PhaxReference()
        service._request_model_tool = AsyncMock()
        result = await service._submit_action_clauses_impl("Have Codex fix that", "reference-test")
        self.assertEqual(result["status"], "failed")
        service._request_model_tool.assert_not_called()
