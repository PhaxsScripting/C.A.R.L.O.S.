"""Small local style learner from direct user feedback, never tool/page text.

Only a bounded signed response-length score is stored. No copied utterances,
inferred identity, permissions, executable aliases or automatic OS changes.
"""

import re


class StyleLearner:
    def __init__(self, daily):
        self.daily = daily

    def snapshot(self):
        record = self.daily.records("learned_style").get("response_length", {})
        if not isinstance(record, dict):
            record = {}
        score = record.get("score", 0)
        count = record.get("feedback_count", 0)
        score = max(-6, min(6, score)) if type(score) is int else 0
        count = max(0, min(1000, count)) if type(count) is int else 0
        enabled = record.get("enabled", True) is True
        return {
            "enabled": enabled,
            "score": score,
            "feedback_count": count,
            "response_length_hint": (
                "short"
                if enabled and score <= -3
                else "detailed" if enabled and score >= 3 else None
            ),
            "source": "direct user response-length feedback",
            "raw_utterances_stored": False,
            "changes_execution_policy": False,
        }

    def _save(self, record):
        expected = {k: record[k] for k in ("score", "feedback_count", "enabled")}
        self.daily.save("learned_style", "response_length", expected)
        if self.daily.records("learned_style").get("response_length") != expected:
            raise RuntimeError("Local style feedback could not be verified after saving")

    def observe_user_feedback(self, text):
        state = self.snapshot()
        if not state["enabled"] or not isinstance(text, str) or len(text) > 120:
            return state
        text = re.sub(r"\s+", " ", text.casefold().strip()).strip(".!?, ")
        text = re.sub(r"^(?:please |can you |could you )", "", text)
        text = re.sub(r"(?: please| thanks)$", "", text).strip(" ,")
        short = re.fullmatch(
            r"(?:shorter|keep (?:it|your (?:answers|replies)) short|less detail|too much detail|be (?:brief|concise)|stop (?:being so verbose|overexplaining))",
            text,
        )
        detailed = re.fullmatch(
            r"(?:more detail|be more detailed|explain (?:more|in more detail)|expand (?:that|your answer)|too brief|too short)",
            text,
        )
        if not short and not detailed:
            return state
        state["score"] = max(-6, min(6, state["score"] + (-1 if short else 1)))
        state["feedback_count"] = min(1000, state["feedback_count"] + 1)
        self._save(state)
        return self.snapshot()

    def configure(self, *, enabled=None, reset=False):
        state = self.snapshot()
        if enabled is not None:
            if type(enabled) is not bool:
                raise ValueError("enabled must be boolean")
            state["enabled"] = enabled
        if reset:
            state.update(score=0, feedback_count=0)
        self._save(state)
        return self.snapshot()
