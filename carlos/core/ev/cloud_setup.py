from __future__ import annotations

import getpass
import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path

from .ai.openai_responses import OpenAIResponsesProvider
from .ai.nvidia import NvidiaProvider
from .config import load_config
from .paths import Paths


def _replace_private(path: Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".ev-config-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


async def setup_openai(paths: Paths, model: str) -> dict[str, str]:
    return await _setup_cloud(paths, model, False)


async def setup_nvidia(paths: Paths, model: str) -> dict[str, str]:
    return await _setup_cloud(paths, model, True)


async def _setup_cloud(paths: Paths, model: str, nvidia: bool) -> dict[str, str]:
    if not re.fullmatch(r"[a-zA-Z0-9_./:-]{1,120}" if nvidia else r"[a-zA-Z0-9_.:-]{1,120}", model):
        raise ValueError("Invalid model name")
    label = "NVIDIA" if nvidia else "OpenAI"
    env_name = "NVIDIA_API_KEY" if nvidia else "OPENAI_API_KEY"
    provider_name = "nvidia" if nvidia else "openai_compatible"
    print(
        f"Cloud conversation sends your text, recent chat, relevant saved memories and tool results to {label}. Microphone audio stays local. Account usage limits and billing terms apply."
    )
    print(
        "Enter the API key here, not in chat. The key stays hidden and is saved privately on this computer."
    )
    key = getpass.getpass(f"{label} API key: ").strip()
    if not re.fullmatch(
        r"nvapi-[A-Za-z0-9_-]{16,1000}" if nvidia else r"sk-[A-Za-z0-9_-]{16,1000}", key
    ):
        raise ValueError("No valid API key was entered; configuration was not changed")
    config = load_config(paths)
    credentials = paths.config_dir / "provider.env"
    if credentials.exists() or credentials.is_symlink():
        metadata = credentials.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
        ):
            raise ValueError("provider.env must be a regular file private to your user")
        previous_lines = credentials.read_text(encoding="utf-8").splitlines()
    else:
        previous_lines = []
    provider_config = {
        "base_url": "https://api.openai.com/v1",
        "model": model,
        "api_key_env": "OPENAI_API_KEY",
        "timeout_seconds": 15,
        "turn_timeout_seconds": 25,
        "max_output_tokens": 800,
        "reasoning_effort": "none" if model == "gpt-5.4-mini" else "",
    }
    if nvidia:
        provider_config.update(
            base_url="https://integrate.api.nvidia.com/v1",
            api_key_env=env_name,
            max_output_tokens=512,
        )
        provider_config.pop("reasoning_effort", None)
    previous_key = os.environ.get(env_name)
    provider = (
        NvidiaProvider(provider_config) if nvidia else OpenAIResponsesProvider(provider_config)
    )
    try:
        os.environ[env_name] = key
        response = await provider.begin("Reply with the single word ready.", [], [], [])
        if not response.text or response.tool_calls:
            raise ValueError("The API connection test returned no usable answer")
    finally:
        await provider.close()
        if previous_key is None:
            os.environ.pop(env_name, None)
        else:
            os.environ[env_name] = previous_key
    backup = Path(tempfile.mkdtemp(prefix="cloud-setup-", dir=paths.state_dir))
    shutil.copy2(paths.config_file, backup / "config.json")
    if credentials.exists():
        shutil.copy2(credentials, backup / "provider.env")
    kept = [line for line in previous_lines if not line.startswith(env_name + "=")]
    try:
        _replace_private(credentials, "\n".join(kept + [env_name + "=" + key]) + "\n")
        config["providers"][provider_name] = provider_config
        config["providers"]["active"] = provider_name
        _replace_private(paths.config_file, json.dumps(config, indent=2) + "\n")
    except BaseException:
        if (backup / "provider.env").exists():
            _replace_private(credentials, (backup / "provider.env").read_text(encoding="utf-8"))
        else:
            credentials.unlink(missing_ok=True)
        raise
    routing = (
        "NVIDIA interprets requests; validated tools execute locally."
        if nvidia
        else "Direct commands still run locally."
    )
    return {
        "status": "configured",
        "model": model,
        "backup": str(backup),
        "next": "Restart E.V. to use the cloud brain: evctl stop, then ev-activate. " + routing,
    }
