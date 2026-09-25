from __future__ import annotations

from ev.platform import executable as _platform_executable

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from .paths import Paths

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "assistant": {
        "name": "Carlos",
        "wake_enabled": False,
        "speak_responses": True,
    },
    "carlos": {
        "schema_version": 1,
        "mode": "DAILY",
        "privacy_mode": "NORMAL",
        "strict_permissions": True,
    },
    "personality": {
        "response_length": "normal",
        "tone": "natural",
        "working_verbosity": "minimal",
        "acknowledgements": "important_only",
        "technical_language": "balanced",
        "voice_expressiveness": 0.62,
    },
    "telemetry": {
        "idle_interval_seconds": 3.0,
        "history_limit": 300,
        "warning_temperature_celsius": 90.0,
        "warning_repeat_seconds": 120.0,
        "conservation_available_percent": 15.0,
        "critical_available_percent": 8.0,
    },
    "notifications": {
        "enabled": False,
        "minimum_repeat_seconds": 90.0,
    },
    "accessibility": {
        "auto_enable_session": True,
    },
    "vision": {
        "local_model": {
            "enabled": False,
            "binary": str(
                Path.home() / ".local/share/ev/runtime/llama-b10793/llama-b10793/llama-server"
            ),
            "model_path": str(
                Path.home()
                / ".local/share/ev/models/qwen3.5-2b-candidate/Qwen_Qwen3.5-2B-Q4_K_M.gguf"
            ),
            "mmproj_path": str(
                Path.home()
                / ".local/share/ev/models/qwen3.5-2b-candidate/mmproj-Qwen_Qwen3.5-2B-f16.gguf"
            ),
            "model": "Qwen3.5-2B-Q4_K_M",
            "context_size": 4096,
        },
        "ocr_python": str(Path.home() / ".local/share/ev/runtime/speech-venv/bin/python"),
        "ocr_timeout_seconds": 30,
        "ocr_nice": 15,
    },
    "ipc": {
        "max_message_bytes": 1_048_576,
        "client_queue_size": 512,
    },
    "memory": {
        "conversation_turn_limit": 40,
        "context_character_limit": 24_000,
    },
    "providers": {
        "active": "offline",
        "offline": {"model": "ev-safe-local"},
        "local_llama": {
            "binary": str(
                Path.home() / ".local/share/ev/runtime/llama-b10793/llama-b10793/llama-server"
            ),
            "model_path": str(
                Path.home()
                / ".local/share/ev/models/qwen2.5-1.5b-instruct/qwen2.5-1.5b-instruct-q4_k_m.gguf"
            ),
            "model": "qwen2.5-1.5b-instruct-q4_k_m.gguf",
            "host": "127.0.0.1",
            "port": 18080,
            "context_size": 4096,
            # Three threads used 25% less generation CPU than four in the
            # local benchmark, with only ~0.3 s added for a 48-token reply.
            "threads": 3,
            "thermal_ceiling_celsius": 93,
            "minimum_available_memory_mib": 512,
            "threads_batch": 3,
            "threads_http": 2,
            "poll": 0,
            "poll_batch": False,
            # This Comet Lake iGPU is slower than CPU inference with this model.
            # Zero layers also disables separate host-op offload in the adapter.
            "gpu_layers": 0,
            "nice": 15,
            "temperature": 0.6,
            "max_output_tokens": 384,
            "casual_max_output_tokens": 96,
            "prompt_cache": True,
            "cache_reuse_tokens": 64,
            "prewarm": True,
            "load_timeout_seconds": 60,
            "request_timeout_seconds": 120,
        },
        "nvidia": {
            "base_url": "https://integrate.api.nvidia.com/v1",
            "model": "nvidia/nemotron-3-super-120b-a12b",
            "api_key_env": "NVIDIA_API_KEY",
            "timeout_seconds": 15,
            "turn_timeout_seconds": 25,
            "max_output_tokens": 512,
        },
        "openai_compatible": {
            "base_url": "https://api.openai.com/v1",
            "model": "",
            "api_key_env": "OPENAI_API_KEY",
            "timeout_seconds": 60,
        },
    },
    "security": {
        "approval_mode": "codex_only",
        "confirmation_timeout_seconds": 90,
        "allowed_roots": [
            str(Path.home() / "Downloads"),
            str(Path.home() / "Documents"),
            str(Path.home() / "Desktop"),
            str(Path.home() / "Pictures"),
        ],
        "max_file_read_bytes": 65_536,
        "max_tool_output_bytes": 262_144,
    },
    "voice": {
        "microphone_source": "@DEFAULT_SOURCE@",
        "input_highpass_hz": 140,
        "capture_command": [
            _platform_executable("/usr/bin/parec"),
            "--raw",
            "--format=s16le",
            "--rate=16000",
            "--channels=1",
            "--latency-msec=20",
            "--process-time-msec=20",
        ],
        "stt_provider": "whisper_cpp",
        "max_capture_seconds": 60,
        "stt": {
            "provider": "whisper_cpp",
            "binary": str(
                Path.home()
                / ".local/share/ev/runtime/whisper-b4938/whisper-bin-ubuntu-x64/whisper-cli"
            ),
            "model": str(Path.home() / ".local/share/ev/models/whisper/ggml-base.en.bin"),
            "verification_model": str(
                Path.home() / ".local/share/ev/models/whisper/ggml-small.en-q5_1.bin"
            ),
            "language": "en",
            "threads": 4,
            "beam_size": 3,
            "verification_threads": 4,
            "nice": 15,
            "verification_nice": 15,
            "persistent_server": True,
            "server_binary": str(
                Path.home()
                / ".local/share/ev/runtime/whisper-b4938/whisper-bin-ubuntu-x64/whisper-server"
            ),
            "server_port": 18082,
            "server_start_timeout_seconds": 15,
            "timeout_seconds": 45,
            "verification_timeout_seconds": 60,
            "prompt": "English conversation and desktop requests. Pause my music. Unpause Spotify. Open Firefox. Lower the volume. Phaxity Neko Music.",
            "verification_prompt": "Verify this English desktop command exactly. Application names include Phaxity Neko Music, Phaxity Audio, Spotify, Firefox, Discord, Steam, and Visual Studio Code.",
        },
        "vad": {
            "neural_enabled": True,
            "python": str(Path.home() / ".local/share/ev/runtime/speech-venv/bin/python"),
            "model": str(Path.home() / ".local/share/ev/models/vad/silero-v6.onnx"),
            "maximum_noise_floor": 0.020,
            "speech_probability": 0.45,
            "continuation_probability": 0.25,
            "automatic_end": True,
            "minimum_rms": 0.010,
            "noise_ratio": 3.0,
            "start_ms": 120,
            "minimum_speech_ms": 180,
            "end_silence_ms": 1600,
            "responsive_end_silence_ms": 1600,
            "maximum_speech_ms": 60000,
            "pre_roll_ms": 450,
            "stt_tail_ms": 500,
        },
        "wake": {
            "enabled": True,
            "python": str(Path.home() / ".local/share/ev/runtime/speech-venv/bin/python"),
            "model_dir": str(
                Path.home()
                / ".local/share/ev/models/wake/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"
            ),
            "keywords": str(Path.home() / ".local/share/ev/app/assets/voice/keywords.txt"),
            "tokens": "tokens.txt",
            "encoder": "encoder-epoch-13-avg-2-chunk-16-left-64.int8.onnx",
            "decoder": "decoder-epoch-13-avg-2-chunk-16-left-64.onnx",
            "joiner": "joiner-epoch-13-avg-2-chunk-16-left-64.int8.onnx",
            "threads": 1,
            "nice": 15,
            "score": 1.5,
            "threshold": 0.30,
            "media_requires_hey": True,
            "pause_media_on_wake": True,
            "speech_backup": False,
            "acknowledgement_chime": True,
            "candidate_cooldown_seconds": 1.2,
            "rolling_buffer_ms": 2200,
            "gate_minimum_rms": 0.006,
            # Only reject truly saturated/stuck inputs. Normal laptop-array
            # speech can legitimately sit well above 0.18 RMS.
            "gate_maximum_rms": 0.75,
            "gate_maximum_dc_offset": 0.5,
            "gate_peak": 0.02,
            "gate_analysis_ms": 100,
            "gate_pre_roll_ms": 350,
            "gate_hangover_ms": 1100,
            "invalid_input_hold_ms": 1000,
            "invalid_probe_ms": 250,
            "source_poll_seconds": 1.0,
            "reconnect_seconds": 2.0,
            "command_wait_seconds": 6.0,
        },
        "follow_up": {
            "enabled": True,
            "duration_seconds": 12.0,
        },
        "tts_provider": "espeak_visualized",
        "tts_command": _platform_executable("/usr/bin/spd-say"),
        "tts": {
            "provider": "piper",
            "python": str(Path.home() / ".local/share/ev/runtime/speech-venv/bin/python"),
            "model": str(
                Path.home()
                / ".local/share/ev/models/piper/en_US-hfc_female-medium/en_US-hfc_female-medium.onnx"
            ),
            "model_config": str(
                Path.home()
                / ".local/share/ev/models/piper/en_US-hfc_female-medium/en_US-hfc_female-medium.onnx.json"
            ),
            "voice": "E.V. American",
            "speaking_rate": 1.00,
            "volume": 0.88,
            "noise_scale": 0.50,
            "noise_w_scale": 0.65,
            "sentence_silence": 0.20,
            "output_device": "@DEFAULT_SINK@",
            "fallback_voice": "en-us+f3",
            "thermal_pause_celsius": 97.0,
            "nice": 15,
            "timeout_seconds": 30,
            "persistent_worker": True,
            "worker_start_timeout_seconds": 15,
        },
    },
}


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(paths: Paths) -> dict[str, Any]:
    from .platform.config_defaults import defaults

    base = defaults(DEFAULT_CONFIG)
    paths.ensure()
    if not paths.config_file.exists():
        text = json.dumps(base, indent=2) + "\n"
        descriptor = os.open(paths.config_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        return deepcopy(base)
    with paths.config_file.open(encoding="utf-8") as handle:
        local = json.load(handle)
    if not isinstance(local, dict):
        raise ValueError("E.V. config root must be an object")
    os.chmod(paths.config_file, 0o600)
    return _merge(base, local)
