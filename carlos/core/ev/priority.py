"""Deterministic event importance; model text cannot grant interrupt priority."""


def priority_for(kind, source, payload):
    if kind == "system.warning" and source == "telemetry" and payload.get("kind") == "thermal":
        value = payload.get("celsius")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 98:
            return "EMERGENCY"
        return "HIGH"
    if kind in {"system.error", "system.warning", "security.alert", "voice.full_test_failed"}:
        return "HIGH"
    if kind == "tool.permission_check" and payload.get("decision") == "PENDING":
        return "HIGH"
    if kind in {
        "tool.completed",
        "system.telemetry",
        "voice.audio_level",
        "tts.audio_level",
        "agent.insights_changed",
        "download.completed",
        "coding.completed",
    }:
        return "BACKGROUND"
    return "NORMAL"
