from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    config_dir: Path
    data_dir: Path
    state_dir: Path
    cache_dir: Path
    runtime_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.json"

    @property
    def database(self) -> Path:
        return self.data_dir / "memory.db"

    @property
    def log_file(self) -> Path:
        return self.state_dir / "ev-core.jsonl"

    @property
    def socket(self) -> Path:
        return self.runtime_dir / "ev.sock"

    @property
    def lock_file(self) -> Path:
        return self.runtime_dir / "ev-core.lock"

    def ensure(self) -> None:
        for path in (
            self.config_dir,
            self.data_dir,
            self.state_dir,
            self.cache_dir,
            self.runtime_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        os.chmod(self.config_dir, 0o700)
        os.chmod(self.data_dir, 0o700)
        os.chmod(self.state_dir, 0o700)
        os.chmod(self.runtime_dir, 0o700)


def get_paths() -> Paths:
    home = Path.home()
    runtime_base = Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/ev-runtime-{os.getuid()}"))
    return Paths(
        config_dir=Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "ev",
        data_dir=Path(os.environ.get("XDG_DATA_HOME", home / ".local/share")) / "ev",
        state_dir=Path(os.environ.get("XDG_STATE_HOME", home / ".local/state")) / "ev",
        cache_dir=Path(os.environ.get("XDG_CACHE_HOME", home / ".cache")) / "ev",
        runtime_dir=runtime_base / "ev",
    )
