"""Public identity, with stable legacy storage and IPC identifiers."""

NAME = "Carlos"
SYSTEM = "C.A.R.L.O.S."
EXPANSION = "Crackhead Artificial Robot Living On Shitbox"
API_VERSION = 1
STATE_NAMES = {
    "AWAKE": "ATTENTION",
    "USING_TOOL": "EXECUTING",
    "WAITING_FOR_CONFIRMATION": "WAITING_FOR_USER",
    "RETRIEVING_MEMORY": "THINKING",
    "TRANSCRIBING": "LISTENING",
}


def identity():
    return {
        "name": NAME,
        "system": SYSTEM,
        "expansion": EXPANSION,
        "api_version": API_VERSION,
        "compatibility": {
            "package": "ev",
            "dbus": "com.ev.Core",
            "socket": "ev/ev.sock",
            "storage": "ev",
        },
    }


def event_name(kind, payload):
    if kind == "core.state_changed":
        return (
            "carlos." + STATE_NAMES.get(payload.get("to", ""), payload.get("to", "unknown")).lower()
        )
    return {
        "core.started": "carlos.started",
        "core.stopping": "carlos.stopping",
        "voice.barge_in": "carlos.interrupted",
        "voice.transcription_complete": "voice.final_transcript",
        "tts.started": "voice.audio_started",
        "tts.cancelled": "voice.cancelled",
    }.get(kind, kind)
