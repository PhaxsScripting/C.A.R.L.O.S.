"""Native executable defaults; never rewrite an existing user's configuration."""

import sys
from pathlib import Path
from copy import deepcopy
from .system import IS_FREEBSD


def defaults(shared):
    result = deepcopy(shared)
    if not IS_FREEBSD:
        return result
    native = Path.home() / ".local/share/ev/runtime/freebsd/bin"
    result["providers"]["local_llama"]["binary"] = "/usr/local/bin/llama-server"
    result["vision"]["local_model"]["binary"] = "/usr/local/bin/llama-server"
    result["vision"]["ocr_python"] = str(native / "python")
    voice = result["voice"]
    voice["stt"]["binary"] = "/usr/local/bin/whisper-cli"
    voice["stt"]["server_binary"] = "/usr/local/bin/whisper-server"
    for section in ("wake", "tts", "vad"):
        voice[section]["python"] = str(native / "python")
    voice["tts"]["model"] = str(
        Path.home() / ".local/share/ev/models/piper/en_GB-alan-medium/en_GB-alan-medium.onnx"
    )
    voice["tts"]["model_config"] = voice["tts"]["model"] + ".json"
    voice["tts"]["voice"] = "E.V. Alan"
    return result
