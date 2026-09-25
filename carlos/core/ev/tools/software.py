"""Bounded Gentoo package evidence; no ebuild execution or installation.

VDB records establish recorded installation, not executable health. Repository
cache records establish local metadata presence, not trusted/eligible packages.
"""

import os
import re
import shutil
import stat
import time
from pathlib import Path

from ..permissions import Permission
from .base import ToolSpec, ValidationError
from .builtin import object_schema

VDB_ROOT = Path("/var/db/pkg")
REPOSITORY_ROOT = Path("/var/db/repos")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9+_.-]{0,149}\Z")
_CPV = re.compile(r"(.+)-([0-9][A-Za-z0-9._-]*)\Z")


def _metadata(path):
    """Never block on special files or follow the final symlink."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            raise ValueError("Package metadata is not a bounded regular file")
        raw = os.read(descriptor, 65537)
        if len(raw) > 65536:
            raise ValueError("Package metadata exceeded its read budget")
        return raw.decode("utf-8", errors="replace")
    finally:
        os.close(descriptor)


def _entries(path, limit):
    """Bound directory enumeration before sorting; caller reports truncation."""
    with os.scandir(path) as listing:
        entries = []
        for entry in listing:
            entries.append(entry)
            if len(entries) > limit:
                return entries[:limit], True
    return sorted(entries, key=lambda entry: entry.name), False


def software_search(arguments, _context):
    query = arguments["query"].strip().casefold()
    if not re.fullmatch(r"[a-z0-9][a-z0-9+_./ -]{1,99}", query) or ".." in query:
        raise ValidationError("Use 2-100 package-name terms, not paths, commands or URLs")
    scope = arguments.get("scope", "installed")
    limit = arguments.get("limit", 20)
    if (
        scope not in {"installed", "repository", "both"}
        or type(limit) is not int
        or not 1 <= limit <= 30
    ):
        raise ValidationError("Invalid package search scope or limit")
    terms = query.split()
    deadline = time.monotonic() + 2
    result, scanned, limited, errors = [], 0, False, 0
    sources, unavailable = [], []
    if scope in {"installed", "both"}:
        sources.append(("installed", VDB_ROOT, None))
    if scope in {"repository", "both"}:
        try:
            repos, overflow = _entries(REPOSITORY_ROOT, 32)
            limited |= overflow
            for repo in repos:
                if _NAME.fullmatch(repo.name) and repo.is_dir(follow_symlinks=False):
                    cache = Path(repo.path) / "metadata/md5-cache"
                    if cache.is_dir() and not cache.is_symlink() and not cache.parent.is_symlink():
                        sources.append(("repository", cache, repo.name))
        except OSError:
            unavailable.append("repository_cache")
        if not any(source[0] == "repository" for source in sources):
            unavailable.append("no_local_repository_cache")
    for kind, root, repository in sources:
        if limited or len(result) >= limit or time.monotonic() >= deadline:
            limited = True
            break
        try:
            if root.is_symlink():
                raise OSError("Symlinked metadata root is not traversed")
            categories, overflow = _entries(root, 400)
            limited |= overflow
            for category in categories:
                if not _NAME.fullmatch(category.name) or not category.is_dir(follow_symlinks=False):
                    continue
                entries, overflow = _entries(category.path, 5000)
                limited |= overflow
                for entry in entries:
                    scanned += 1
                    if scanned > 60000 or time.monotonic() >= deadline or len(result) >= limit:
                        limited = True
                        break
                    if not _NAME.fullmatch(entry.name):
                        continue
                    match = _CPV.fullmatch(entry.name)
                    if not match:
                        continue
                    package, version = match.groups()
                    atom = category.name + "/" + package
                    if not all(term in atom.casefold() for term in terms):
                        continue
                    if not (
                        entry.is_dir(follow_symlinks=False)
                        if kind == "installed"
                        else entry.is_file(follow_symlinks=False)
                    ):
                        continue
                    row = {
                        "atom": atom,
                        "version": version,
                        "source": kind,
                        "recorded_installed": kind == "installed",
                        "repository": repository,
                        "installable": None,
                        "runtime_working": None,
                    }
                    try:
                        if kind == "installed":
                            row["slot"] = _metadata(Path(entry.path) / "SLOT").strip()[:200]
                            row["repository"] = _metadata(Path(entry.path) / "repository").strip()[
                                :200
                            ]
                        else:
                            data = dict(
                                line.split("=", 1)
                                for line in _metadata(entry.path).splitlines()
                                if "=" in line
                            )
                            row.update(
                                {
                                    key.lower(): data.get(key, "")[:500]
                                    for key in (
                                        "DESCRIPTION",
                                        "HOMEPAGE",
                                        "LICENSE",
                                        "SLOT",
                                        "KEYWORDS",
                                    )
                                }
                            )
                    except (OSError, ValueError):
                        errors += 1
                        row["metadata_incomplete"] = True
                    result.append(row)
                if limited:
                    break
        except OSError:
            unavailable.append(f"{kind}:{repository or 'vdb'}")
    return {
        "query": query,
        "scope": scope,
        "packages": result,
        "count": len(result),
        "scanned": scanned,
        "partial": limited or bool(errors or unavailable),
        "metadata_errors": errors,
        "unavailable_sources": unavailable,
        "backend": "Gentoo VDB and local repository metadata cache",
        "package_manager_installed": shutil.which("emerge") is not None,
        "captured_at_monotonic": time.monotonic(),
        "changed": False,
        "network_requests": 0,
        "untrusted_external_content": True,
        "note": "Local records only. Repository metadata may be stale, masked, incompatible or from an untrusted overlay; no dependency resolution, signature/trust verification, install, update or script execution occurred. Installed records do not prove an app works. Narrow partial searches; empty partial results do not prove absence.",
    }


def register_software_tools(registry):
    registry.register(
        ToolSpec(
            "software.search",
            "SOFTWARE",
            "Discover Gentoo software package records by package/category name terms. Inspect recorded installed versions or locally cached repository candidates, slots and descriptions. Local read-only evidence, no network, package scripts or installation. Repository candidates are NOT proven trusted, unmasked, compatible, current or installable. Apps outside Portage are not covered; use applications.list for desktop launchers. Narrow partial scans.",
            Permission.SAFE,
            object_schema(
                {
                    "query": {"type": "string", "minLength": 2, "maxLength": 100},
                    "scope": {"type": "string", "enum": ["installed", "repository", "both"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 30},
                },
                ["query"],
            ),
            software_search,
            read_only=True,
        )
    )
