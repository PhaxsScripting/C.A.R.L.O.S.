"""Confirmed, revision-checked single replacements with private recovery copies.

This protects against stale reads and ordinary edits, not a hostile same-UID
process racing the final path check and atomic replacement. No file locking
protocol can force unrelated applications to cooperate.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import tempfile
import threading
import time
from pathlib import Path

from ..permissions import Permission
from .base import ToolSpec, ValidationError
from .builtin import object_schema, resolve_allowed


def _identity(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
        info.st_nlink,
    )


def _snapshot(path, maximum):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
            raise ValidationError(
                "Text editing requires a regular, non-hardlinked file within the read-size limit"
            )
        data = source.read(maximum + 1)
        after = os.fstat(source.fileno())
    if (
        len(data) > maximum
        or _identity(before) != _identity(after)
        or _identity(after) != _identity(path.lstat())
    ):
        raise ValidationError("File changed while reading; inspect it again before editing")
    return data, after


def _write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def _replace(arguments, context, cancelled):
    deadline = time.monotonic() + 25

    def guard():
        if cancelled.is_set() or time.monotonic() >= deadline:
            raise ValidationError("Text edit cancelled before replacement")

    guard()
    requested = Path(arguments["path"]).expanduser()
    if requested.is_symlink():
        raise ValidationError("Text editing symbolic links is not supported")
    path = resolve_allowed(str(requested), context)
    maximum = min(int(context.config["security"]["max_file_read_bytes"]), 65536)
    data, identity = _snapshot(path, maximum)
    original_hash = hashlib.sha256(data).hexdigest()
    if original_hash != arguments["expected_sha256"]:
        raise ValidationError("File revision does not match expected_sha256; read/hash it again")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValidationError("Only UTF-8 text files can be edited") from error
    if "\0" in text or "\0" in arguments["new_text"]:
        raise ValidationError("Binary/NUL content cannot be edited")
    old = arguments["old_text"]
    if not old or text.count(old) != 1:
        raise ValidationError("old_text must match exactly once; provide more surrounding context")
    updated = text.replace(old, arguments["new_text"], 1).encode("utf-8")
    if len(updated) > maximum or updated == data:
        raise ValidationError("Replacement exceeds the size limit or does not change the file")
    guard()
    # Keep the backup even if a later step fails. Dont auto-delete recovery files.
    backup_directory = Path(tempfile.mkdtemp(prefix=".ev-text-backup-", dir=path.parent))
    backup = backup_directory / "original"
    _write_private(backup, data)
    if backup.read_bytes() != data:
        raise ValidationError(
            f"Backup verification failed; original file unchanged. Backup: {backup}"
        )
    try:
        with tempfile.TemporaryDirectory(prefix=".ev-text-stage-", dir=path.parent) as temporary:
            stage = Path(temporary) / "replacement"
            _write_private(stage, updated)
            os.chmod(stage, stat.S_IMODE(identity.st_mode) & 0o777)
            if stage.read_bytes() != updated:
                raise ValidationError("Staged replacement verification failed")
            guard()
            current, latest_identity = _snapshot(path, maximum)
            if current != data or _identity(identity) != _identity(latest_identity):
                raise ValidationError("File changed before replacement; no edit applied")
            guard()
            os.replace(stage, path)
        actual, _ = _snapshot(path, maximum)
        verified = actual == updated
        return {
            "verified": verified,
            "path": str(path),
            "backup": str(backup),
            "original_sha256": original_hash,
            "sha256": hashlib.sha256(actual).hexdigest(),
            "replacements": 1,
            "bytes": len(actual),
            "verification": "Expected revision, saved original, exact staged and final byte readback",
            **(
                {}
                if verified
                else {"error": "File did not match after replacement; recovery backup retained"}
            ),
        }
    except Exception as error:
        raise ValidationError(
            f"Text edit failed or is unverified: {error}. Recovery backup: {backup}"
        ) from error


async def replace_text(arguments, context):
    cancelled = threading.Event()
    try:
        return await asyncio.to_thread(_replace, arguments, context, cancelled)
    finally:
        cancelled.set()


def register_text_edit_tools(registry):
    registry.register(
        ToolSpec(
            "files.text_replace",
            "FILES",
            "Replace exactly one occurrence in an existing UTF-8 text file (up to 64 KiB). First read the file and obtain files.hash; supply that exact expected SHA-256. Saves a private original backup, refuses stale revisions and ambiguous matches, verifies readback. Requires confirmation; never claim success before the result.",
            Permission.SENSITIVE,
            object_schema(
                {
                    "path": {"type": "string", "minLength": 1, "maxLength": 4096},
                    "expected_sha256": {"type": "string", "pattern": "[a-f0-9]{64}"},
                    "old_text": {"type": "string", "minLength": 1, "maxLength": 65536},
                    "new_text": {"type": "string", "maxLength": 65536},
                },
                ["path", "expected_sha256", "old_text", "new_text"],
            ),
            replace_text,
            requires_confirmation=True,
            confirmation_reason="Replace text in an existing file after checking its revision and preserving a private backup.",
            verification="Expected SHA-256 plus exact final byte readback",
            side_effects=(
                "replaces one existing file atomically",
                "retains a private original backup beside the file",
            ),
        )
    )
