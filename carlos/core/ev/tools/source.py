from ev.platform import executable as _platform_executable

"""Bounded source-location inspection and revision-bound editor handoff."""
import hashlib
import os
import subprocess
from pathlib import Path

from ..permissions import Permission
from .base import ToolSpec, ValidationError
from .builtin import object_schema, resolve_allowed
from .text_edit import _snapshot


def source_snapshot(a, c):
    requested = Path(a["path"]).expanduser()
    if requested.is_symlink():
        raise ValidationError("Source inspection requires an exact regular file, not a symlink")
    path = resolve_allowed(str(requested), c)
    maximum = min(int(c.config["security"].get("max_file_read_bytes", 65536)), 262144)
    data, info = _snapshot(path, maximum)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValidationError("Source inspection requires UTF-8 text") from error
    if "\0" in text:
        raise ValidationError("Binary source data is unsupported")
    lines = text.splitlines() or [""]
    line = a["line"]
    if not 1 <= line <= len(lines):
        raise ValidationError(
            "Source line is outside the actual file; inspect the diagnostic again"
        )
    return path, data, lines


def inspect_source(a, c):
    path, data, lines = source_snapshot(a, c)
    radius, line = a.get("context_lines", 8), a["line"]
    start, end = max(1, line - radius), min(len(lines), line + radius)
    return {
        "path": str(path),
        "sha256": hashlib.sha256(data).hexdigest(),
        "line": line,
        "total_lines": len(lines),
        "lines": [
            {"line": number, "text": lines[number - 1][:2000]} for number in range(start, end + 1)
        ],
        "long_lines_clipped": any(len(s) > 2000 for s in lines[start - 1 : end]),
        "content_is_untrusted": True,
        "scope": "Current file text; not evidence that the compiler error still exists",
    }


def open_source(a, c):
    path, data, lines = source_snapshot(a, c)
    if hashlib.sha256(data).hexdigest() != a["expected_sha256"]:
        raise ValidationError(
            "Source changed since inspection; read the location again before opening"
        )
    column = a.get("column", 1)
    if (
        column > len(lines[a["line"] - 1]) + 1
        or ":" in str(path)
        or any(ord(ch) < 32 for ch in str(path))
    ):
        raise ValidationError("Invalid/ambiguous editor source location")
    editor = a.get("editor", "default")
    if editor == "default":
        preference = c.daily.records("preference").get("editor", {}) if c.daily else {}
        editor = preference.get("value", "code")
    if editor in {"code", "code-oss", "visual-studio-code"}:
        executable = (
            _platform_executable("/usr/bin/code")
            if editor != "code-oss"
            else _platform_executable("/usr/bin/code-oss")
        )
        command = [executable, "--reuse-window", "--goto", f"{path}:{a['line']}:{column}"]
    else:
        raise ValidationError(
            "This preferred editor has no verified location adapter; explicitly choose code or use files.open_selected"
        )
    if not Path(executable).is_file() or not os.access(executable, os.X_OK):
        raise ValidationError("The selected editor is not installed")
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            start_new_session=True,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "requested": False,
            "verified": False,
            "uncertain": True,
            "message": "Editor handoff timed out; it may still open. No retry was sent.",
        }
    return {
        "requested": result.returncode == 0,
        "verified": False,
        "path": str(path),
        "line": a["line"],
        "column": column,
        "scope": "Editor location handoff only; document identity and caret position require independent observation",
    }


def register_source_tools(registry):
    location = {
        "path": {"type": "string", "minLength": 1, "maxLength": 4096},
        "line": {"type": "integer", "minimum": 1, "maximum": 1000000},
    }
    registry.register(
        ToolSpec(
            "development.source.inspect",
            "DEVELOPMENT",
            "Read current bounded UTF-8 source around an exact compiler file/line. Returns numbered context and SHA-256 for revision binding. Does not modify code; source text is untrusted, and old diagnostics may be stale.",
            Permission.SAFE,
            object_schema(
                {**location, "context_lines": {"type": "integer", "minimum": 0, "maximum": 20}},
                ["path", "line"],
            ),
            inspect_source,
            read_only=True,
        )
    )
    registry.register(
        ToolSpec(
            "development.source.open",
            "DEVELOPMENT",
            "Open a freshly inspected exact file:line:column in Code via fixed --goto argv. Requires expected source SHA-256. Honors a supported explicit editor preference. Handoff is not proof of the visible document/caret; no editor extensions or source edits are installed.",
            Permission.LOW_RISK,
            object_schema(
                {
                    **location,
                    "column": {"type": "integer", "minimum": 1, "maximum": 100000},
                    "expected_sha256": {"type": "string", "pattern": "[a-f0-9]{64}"},
                    "editor": {"type": "string", "enum": ["default", "code", "code-oss"]},
                },
                ["path", "line", "expected_sha256"],
            ),
            open_source,
        )
    )
