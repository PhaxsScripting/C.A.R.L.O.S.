"""Preview-first file organization with verified, non-overwriting undo."""

from __future__ import annotations

import hashlib
import os
import time
import uuid
from pathlib import Path

from .base import ToolRegistry, ToolSpec
from .builtin import resolve_allowed, resolve_destination, object_schema
from ..permissions import Permission


def fingerprint(path: Path) -> dict:
    info = path.lstat()
    if path.is_symlink() or not path.is_file() or info.st_size > 50_000_000:
        raise ValueError("Only regular files up to 50 MB are eligible for this preview")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(262144), b""):
            digest.update(chunk)
    after = path.stat()
    if (info.st_ino, info.st_size, info.st_mtime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ValueError("File changed during preview")
    return {"size": info.st_size, "sha256": digest.hexdigest()}


def preview(arguments, context):
    root = resolve_allowed(arguments["path"], context)
    if not root.is_dir():
        raise ValueError("Choose a directory to preview")
    protected = {
        ".git",
        "CMakeLists.txt",
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        "settings.gradle.kts",
        "pyproject.toml",
        "package.json",
        "AndroidManifest.xml",
    }
    if "AndroidStudioProjects" in root.parts or any(
        (root / marker).exists() for marker in protected
    ):
        raise ValueError(
            "Project folders are excluded; choose a non-project downloads or media folder"
        )
    groups = {
        "Images": {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"},
        "Documents": {".pdf", ".txt", ".md", ".docx", ".odt", ".csv"},
        "Archives": {".zip", ".rar", ".7z", ".gz", ".xz"},
        "Audio": {".mp3", ".flac", ".wav", ".ogg", ".m4a"},
        "Videos": {".mp4", ".mkv", ".webm", ".mov"},
    }
    moves, skipped, hashes = [], [], {}
    total_bytes = 0
    for path in sorted(root.iterdir())[:2000]:
        if path.name.startswith(".") or path.is_symlink() or not path.is_file():
            continue
        group = next(
            (group for group, suffixes in groups.items() if path.suffix.lower() in suffixes), None
        )
        if not group:
            continue
        target = root / group / path.name
        if target.exists() or target.is_symlink() or (root / group).is_symlink():
            skipped.append({"path": str(path), "reason": "destination exists or is a symlink"})
            continue
        if len(moves) >= 200 or total_bytes + path.stat().st_size > 500_000_000:
            skipped.append({"path": str(path), "reason": "preview resource limit"})
            continue
        try:
            identity = fingerprint(path)
        except (OSError, ValueError) as error:
            skipped.append({"path": str(path), "reason": str(error)})
            continue
        total_bytes += identity["size"]
        hashes.setdefault(identity["sha256"], []).append(str(path))
        moves.append({"source": str(path), "destination": str(target), **identity})
    identifier = uuid.uuid4().hex
    record = {
        "id": identifier,
        "root": str(root),
        "created": time.time(),
        "state": "preview",
        "moves": moves,
    }
    context.daily.save("organization", identifier, record)
    return {
        "preview_id": identifier,
        "moves": moves,
        "skipped": skipped[:50],
        "duplicate_groups": [paths for paths in hashes.values() if len(paths) > 1],
        "changed_files": False,
        "message": f"Preview only: {len(moves)} files can be grouped into folders. No files moved. Apply organization {identifier} to execute this exact preview.",
    }


def apply(arguments, context, undo=False):
    identifier = arguments["preview_id"]
    record = context.daily.records("organization").get(identifier)
    if not record or record["state"] != ("applied" if undo else "preview"):
        raise ValueError("No applicable organization preview or undo record")
    if not undo and time.time() - record["created"] > 3600:
        raise ValueError("Organization preview expired; create a new one")
    moves = [
        (
            {**move, "source": move["destination"], "destination": move["source"]}
            if undo
            else dict(move)
        )
        for move in record["moves"]
    ]
    for move in moves:
        source = resolve_allowed(move["source"], context)
        destination = Path(move["destination"])
        if (
            source != Path(move["source"])
            or resolve_destination(str(destination), context) != destination
        ):
            raise ValueError("A path was redirected since preview; nothing was moved")
        if destination.exists() or destination.is_symlink() or destination.parent.is_symlink():
            raise ValueError("Destination changed or already exists; nothing was overwritten")
        if fingerprint(source) != {key: move[key] for key in ("size", "sha256")}:
            raise ValueError("A file changed since the preview; create a new preview")
    completed = []
    try:
        for move in moves:
            source, destination = Path(move["source"]), Path(move["destination"])
            destination.parent.mkdir(exist_ok=True)
            if destination.parent.is_symlink() or source.is_symlink():
                raise ValueError("A path changed during organization")
            # Same filesystem: link+unlink provides no-overwrite semantics. A
            # destination race fails instead of replacing another user's file.
            os.link(source, destination, follow_symlinks=False)
            if fingerprint(destination) != {
                key: move[key] for key in ("size", "sha256")
            } or not os.path.samestat(source.lstat(), destination.lstat()):
                destination.unlink()
                raise ValueError("File changed during move")
            source.unlink()
            completed.append(move)
    except BaseException:
        for move in reversed(completed):
            source, destination = Path(move["source"]), Path(move["destination"])
            if (
                not source.exists()
                and not source.is_symlink()
                and destination.is_file()
                and not destination.is_symlink()
                and fingerprint(destination) == {key: move[key] for key in ("size", "sha256")}
            ):
                os.link(destination, source, follow_symlinks=False)
                destination.unlink()
        raise
    record["state"] = "undone" if undo else "applied"
    context.daily.save("organization", identifier, record)
    return {
        "verified": True,
        "preview_id": identifier,
        "moved_files": len(completed),
        "message": f"{'Restored' if undo else 'Organized'} {len(completed)} files. No existing destination was overwritten.",
    }


def register_organization_tools(registry: ToolRegistry):
    path = {"type": "string", "minLength": 1, "maxLength": 4096}
    token = {"type": "string", "pattern": "[a-f0-9]{32}"}
    registry.register(
        ToolSpec(
            "files.organize.preview",
            "FILES",
            "Preview grouping up to 200 immediate regular files by type, report duplicate hashes, and store an exact expiring plan. Does not touch projects or directories.",
            Permission.SAFE,
            object_schema({"path": path}, ["path"]),
            preview,
            timeout_seconds=30,
        )
    )
    registry.register(
        ToolSpec(
            "files.organize.apply",
            "FILES",
            "Execute one exact organization preview after rechecking every file hash and destination; never overwrite.",
            Permission.SENSITIVE,
            object_schema({"preview_id": token}, ["preview_id"]),
            apply,
            timeout_seconds=120,
            cancellable=False,
        )
    )
    registry.register(
        ToolSpec(
            "files.organize.undo",
            "FILES",
            "Undo an applied file-organization plan only if its files remain unchanged and original paths are vacant.",
            Permission.SENSITIVE,
            object_schema({"preview_id": token}, ["preview_id"]),
            lambda a, c: apply(a, c, undo=True),
            timeout_seconds=120,
            cancellable=False,
        )
    )
