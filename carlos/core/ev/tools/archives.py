"""Bounded ZIP/TAR extraction: inspect, stage, verify, publish without overwrite.

No shell, archive-provided commands, links, executable permissions or extractall.
These same-user filesystem operations are not a sandbox against a hostile UID.
"""

from __future__ import annotations

import asyncio
import ctypes
import gzip
import hashlib
import os
import stat
import tarfile
import tempfile
import threading
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path

from ..permissions import Permission
from .base import ToolSpec, ValidationError
from .builtin import object_schema, resolve_allowed, resolve_destination

MAX_INPUT = 64 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024
MAX_ENTRIES = 2000
MAX_SECONDS = 25


def _check(deadline, cancelled):
    if cancelled.is_set() or time.monotonic() >= deadline:
        raise ValidationError(
            "Archive operation cancelled or exceeded its time budget; nothing published"
        )


def _name(raw):
    # Reject ambiguous Windows and POSIX paths, including normalized-away '..'.
    if (
        not raw
        or len(raw) > 1024
        or raw.startswith("/")
        or "\\" in raw
        or ":" in raw
        or any(ord(c) < 32 for c in raw)
    ):
        raise ValidationError("Unsafe archive member path")
    while raw.startswith("./"):
        raw = raw[2:]
    parts = raw.rstrip("/").split("/")
    if any(p in ("", ".", "..") for p in parts) or len(parts) > 32 or len(raw) > 1024:
        raise ValidationError("Unsafe or excessive archive member path")
    return "/".join(parts)


@contextmanager
def _open_archive(path, deadline, cancelled):
    # O_NOFOLLOW also protects a leaf replaced between resolution and opening.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as raw:
        info = os.fstat(raw.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_INPUT:
            raise ValidationError("Archive must be a regular file no larger than 64 MiB")
        signature = raw.read(4)
        raw.seek(0)
        if signature[:2] == b"PK":
            with zipfile.ZipFile(raw) as archive:
                yield "zip", archive
        elif signature[:2] == b"\x1f\x8b":
            # Bound gzip expansion BEFORE tarfile parses extended/PAX headers.
            with tempfile.TemporaryFile() as expanded, gzip.GzipFile(fileobj=raw) as compressed:
                size = 0
                while chunk := compressed.read(1024 * 1024):
                    _check(deadline, cancelled)
                    size += len(chunk)
                    if size > MAX_EXPANDED:
                        raise ValidationError("Compressed TAR exceeds the 256 MiB expansion limit")
                    expanded.write(chunk)
                expanded.seek(0)
                with tarfile.open(fileobj=expanded, mode="r:") as archive:
                    yield "tar.gz", archive
        else:
            with tarfile.open(fileobj=raw, mode="r:") as archive:
                yield "tar", archive


def _inventory(kind, archive, deadline, cancelled):
    entries, paths, total = [], {}, 0
    members = archive.infolist() if kind == "zip" else archive
    for index, member in enumerate(members):
        _check(deadline, cancelled)
        if index >= MAX_ENTRIES:
            raise ValidationError("Archive exceeds the 2000-entry limit")
        directory = member.is_dir() if kind == "zip" else member.isdir()
        raw_name = member.filename if kind == "zip" else member.name
        # Common `tar -cf archive.tar .` root marker does not create a member.
        if kind != "zip" and raw_name in (".", "./") and directory:
            continue
        name = _name(raw_name)
        size = member.file_size if kind == "zip" else member.size
        if kind == "zip":
            mode = (member.external_attr >> 16) & 0xFFFF
            allowed_type = stat.S_IFDIR if directory else stat.S_IFREG
            if stat.S_IFMT(mode) not in (0, allowed_type) or member.flag_bits & 1:
                raise ValidationError(
                    "Encrypted, linked or special archive members are not supported"
                )
            if member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise ValidationError("Unsupported ZIP compression method")
            if size > max(1024 * 1024, member.compress_size * 200):
                raise ValidationError("ZIP member exceeds the expansion-ratio limit")
        elif not (member.isfile() or directory) or member.issparse():
            raise ValidationError("TAR links, sparse and special files are not supported")
        if name in paths or size < 0 or (directory and size):
            raise ValidationError("Duplicate member or invalid archive size")
        total += size
        if total > MAX_EXPANDED:
            raise ValidationError("Archive exceeds the 256 MiB expanded-size limit")
        paths[name] = directory
        entries.append((name, directory, size, member))
    for name in paths:
        if any(
            paths.get(str(parent)) is False for parent in Path(name).parents if str(parent) != "."
        ):
            raise ValidationError("Archive file/directory paths conflict")
    return entries, total


def _publish(source, destination):
    # Linux atomic directory publication; os.rename would replace an empty dir.
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise ValidationError("Atomic no-overwrite publication is unavailable on this platform")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1):
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), str(destination))


def _operation(arguments, context, cancelled, *, extract):
    deadline = time.monotonic() + MAX_SECONDS
    original = Path(arguments["path"]).expanduser()
    if original.is_symlink():
        raise ValidationError("Archive symbolic links are not supported")
    path = resolve_allowed(arguments["path"], context)
    destination = None
    if extract:
        requested = Path(arguments["destination"]).expanduser()
        if requested.is_symlink():
            raise ValidationError("Destination cannot be a symbolic link")
        destination = resolve_destination(arguments["destination"], context)
        if destination.exists() or not destination.parent.is_dir():
            raise ValidationError("Choose a new destination folder with an existing parent")
    with _open_archive(path, deadline, cancelled) as (kind, archive):
        entries, total = _inventory(kind, archive, deadline, cancelled)
        summary = {
            "path": str(path),
            "format": kind,
            "entries_count": len(entries),
            "expanded_bytes": total,
        }
        if not extract:
            # Metadata inspection does not validate compressed payload CRCs.
            return {
                **summary,
                "payload_verified": False,
                "entries": [
                    {"path": name, "directory": directory, "bytes": size}
                    for name, directory, size, _ in entries[:50]
                ],
                "listing_limited": len(entries) > 50,
            }
        with tempfile.TemporaryDirectory(
            prefix=".ev-extract-", dir=destination.parent
        ) as temporary:
            stage = Path(temporary) / "payload"
            stage.mkdir(mode=0o700)
            digests = {}
            for name, directory, size, member in entries:
                _check(deadline, cancelled)
                target = stage / name
                if directory:
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                source = archive.open(member) if kind == "zip" else archive.extractfile(member)
                digest, written = hashlib.sha256(), 0
                with source, target.open("xb") as output:
                    os.chmod(target, 0o600)
                    while chunk := source.read(min(1024 * 1024, size - written + 1)):
                        _check(deadline, cancelled)
                        written += len(chunk)
                        if written > size:
                            raise ValidationError("Archive payload exceeds its declared size")
                        output.write(chunk)
                        digest.update(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if written != size:
                    raise ValidationError("Archive payload is incomplete")
                digests[name] = digest.hexdigest()
            # Independent staged readback before any destination is published.
            for name, expected in digests.items():
                actual = hashlib.sha256()
                with (stage / name).open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        _check(deadline, cancelled)
                        actual.update(chunk)
                if actual.hexdigest() != expected:
                    raise ValidationError("Extracted file hash verification failed")
            _check(deadline, cancelled)
            _publish(stage, destination)
        return {
            **summary,
            "destination": str(destination),
            "verified": True,
            "files_verified": len(digests),
            "verification": "Complete payload read, staged SHA-256 readback, atomic no-overwrite publication",
            "executable_permissions_preserved": False,
        }


async def _run(arguments, context, *, extract):
    cancelled = threading.Event()
    try:
        return await asyncio.to_thread(_operation, arguments, context, cancelled, extract=extract)
    finally:
        cancelled.set()


async def inspect_archive(arguments, context):
    return await _run(arguments, context, extract=False)


async def extract_archive(arguments, context):
    return await _run(arguments, context, extract=True)


def register_archive_tools(registry):
    path = {"type": "string", "minLength": 1, "maxLength": 4096}
    registry.register(
        ToolSpec(
            "files.archive_inspect",
            "FILES",
            "Inspect ZIP, TAR or TAR.GZ metadata and reject unsafe members. Lists first 50 entries; does not verify payloads. Limits: 64 MiB input, 256 MiB expanded, 2000 entries.",
            Permission.SAFE,
            object_schema({"path": path}, ["path"]),
            inspect_archive,
            read_only=True,
        )
    )
    registry.register(
        ToolSpec(
            "files.archive_extract",
            "FILES",
            "Extract ZIP, TAR or TAR.GZ into a NEW folder under allowed roots. No overwrites, links or executable permissions. Verifies every file before atomic publication. Limits: 64 MiB input, 256 MiB expanded, 2000 entries; RAR/7z unsupported.",
            Permission.LOW_RISK,
            object_schema({"path": path, "destination": path}, ["path", "destination"]),
            extract_archive,
            verification="Staged file SHA-256 readback and no-overwrite publication",
            side_effects=("creates a new directory and files",),
        )
    )
