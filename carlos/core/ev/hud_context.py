"""Short-lived reference to an actually displayed, known engineering failure."""

import time


class PhaxReference:
    def __init__(self):
        self.value = None

    def clear(self):
        self.value = None

    def remember(self, card, proposal_id, now=None):
        now = time.monotonic() if now is None else now
        if (
            card.get("proposal_id") != proposal_id
            or card.get("state") not in {"FAILED", "VALIDATION_FAILED"}
            or not isinstance(card.get("observed_monotonic"), (int, float))
            or not 0 <= now - card["observed_monotonic"] <= 120
        ):
            raise ValueError("No fresh matching failure card")
        self.value = {
            key: card.get(key)
            for key in ("proposal_id", "project", "state", "message", "observed_monotonic")
        }
        return {
            "remembered": True,
            "expires_after_seconds": max(0, 120 - (now - card["observed_monotonic"])),
        }

    def get(self, now=None):
        now = time.monotonic() if now is None else now
        if not self.value or not 0 <= now - self.value["observed_monotonic"] <= 120:
            self.clear()
            raise ValueError(
                "The failure reference has expired. Display a current failure card first."
            )
        return dict(self.value)


# Old integrations still know this name. Let them through.
FailureReference = PhaxReference
