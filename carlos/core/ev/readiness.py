"""Readiness is fresh component evidence, never an installation claim."""

import time


def project_readiness(snapshot, capabilities, startup_seconds=None):
    voice = snapshot.get("voice", {})
    rows = capabilities.get("capabilities", capabilities)

    def observed(name):
        return rows.get(name, {}).get("state", "UNVERIFIED")

    components = {
        "Core": "READY",
        "Voice": (
            "MUTED"
            if voice.get("privacy_mode")
            else (
                "DISABLED"
                if not voice.get("wake_enabled")
                else (
                    "READY"
                    if voice.get("wake_active")
                    and voice.get("stt_available")
                    and voice.get("tts_available")
                    else "STARTING"
                )
            )
        ),
        "Local AI": observed("LocalAI"),
        "Desktop": "READY" if snapshot.get("desktop", {}).get("available") else "UNVERIFIED",
        "Remote": observed("Remote"),
    }
    ready = all(value == "READY" for value in components.values())
    return {
        "state": "FULLY_READY" if ready else "PARTIAL",
        "components": components,
        "observed_at": time.time(),
        "startup_to_ipc_seconds": startup_seconds,
        "boot_to_usable_seconds": None,
        "resume_to_usable_seconds": None,
        "scope": "Process startup and current component observations; OS boot and resume require separate measurements",
    }
