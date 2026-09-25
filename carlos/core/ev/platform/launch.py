"""Private provider loading without sourcing shell code or logging credentials."""

import os
import stat
import sys
from pathlib import Path


def main():
    component = sys.argv[1]
    if component == "core":
        path = (
            Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
            / "ev/provider.env"
        )
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            fd = None
        if fd is not None:
            with os.fdopen(fd) as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_mode & 0o077
                    or info.st_size > 65536
                ):
                    raise SystemExit("E.V. refused insecure provider.env")
                allowed = {
                    "NVIDIA_API_KEY",
                    "OPENAI_API_KEY",
                    "ANTHROPIC_API_KEY",
                    "GEMINI_API_KEY",
                    "GROQ_API_KEY",
                    "OPENROUTER_API_KEY",
                }
                for line in stream:
                    key, sep, value = line.rstrip("\n").partition("=")
                    if sep and key in allowed:
                        os.environ[key] = value
        os.environ["EV_DBUS_NAME"] = "com.ev.Core"
        module = "ev"
    elif component == "cli":
        module = "ev.cli"
    else:
        raise SystemExit("Unknown component")
    os.execv(sys.executable, [sys.executable, "-m", module, *sys.argv[2:]])


if __name__ == "__main__":
    main()
