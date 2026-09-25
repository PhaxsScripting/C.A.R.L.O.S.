from ev.platform import executable as _platform_executable

"""Inspected project commands run with explicit approval inside Bubblewrap.

Only the selected project is writable/persistent; no desktop bus, credentials,
other home files or network are mounted. A zero exit code is process evidence,
not proof of the user's overall development goal.
"""
import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import time
import uuid
from pathlib import Path

from ..permissions import Permission
from .base import ToolSpec, ValidationError
from .builtin import object_schema, resolve_allowed, allowed_roots
from .text_edit import _snapshot, _write_private

MARKERS = {
    "cmake": "CMakeLists.txt",
    "make": "Makefile",
    "cargo": "Cargo.toml",
    "npm": "package.json",
    "python": "pyproject.toml",
}


def validate_mount_tree(root):
    """Reject filesystem objects that defeat a project-only writable mount.

    This is preflight, not isolation from a hostile concurrent host process.
    No symlink traversal and no reads of project source contents.
    """
    deadline, count = time.monotonic() + 3, 0

    def failed(error):
        raise ValidationError("Project tree could not be inspected for safe mounting") from error

    for folder, directories, files in os.walk(root, followlinks=False, onerror=failed):
        for name in [*directories, *files]:
            count += 1
            if count > 50000 or time.monotonic() > deadline:
                raise ValidationError(
                    "Project mount inspection exceeded its budget; use a smaller sanitized workspace"
                )
            info = (Path(folder) / name).lstat()
            if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                raise ValidationError(
                    "Project contains hardlinked files that could share writes outside its sandbox"
                )
            if not (
                stat.S_ISREG(info.st_mode)
                or stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
            ):
                raise ValidationError(
                    "Project contains a socket, device or special file; use a sanitized workspace"
                )


def inspect_project(a, c):
    root = resolve_allowed(a["project"], c)
    if not root.is_dir() or root in allowed_roots(c):
        raise ValidationError(
            "Choose a specific project subdirectory, not an allowed-root/home directory"
        )
    markers, digest = {}, hashlib.sha256()
    for system, name in MARKERS.items():
        path = root / name
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
            raise ValidationError("Project manifest must be a bounded regular file, not a link")
        with path.open("rb") as handle:
            content = handle.read(65537)
        if len(content) > 65536:
            raise ValidationError("Project manifest grew past its inspection limit")
        digest.update(name.encode() + b"\0" + content)
        markers[system] = name
    if not markers:
        raise ValidationError("No supported project manifest found; specify its actual root")
    return {
        "project": str(root),
        "build_systems": list(markers),
        "manifests": markers,
        "manifest_sha256": digest.hexdigest(),
        "git_present": (root / ".git").exists(),
        "sandbox_installed": shutil.which("bwrap") is not None,
        "sandbox_verified_for_this_project": False,
        "build_directory": str(root / "build" / "ev-agent") if "cmake" in markers else None,
        "execution_requires_approval": True,
        "scope": "Manifest detection and hash, not project execution or dependency verification",
    }


def project_command(a, c):
    observed = inspect_project(a, c)
    if observed["manifest_sha256"] != a["expected_manifest_sha256"]:
        raise ValidationError("Project manifests changed since inspection/approval; inspect again")
    root, system, operation = Path(observed["project"]), a["build_system"], a["operation"]
    if system not in observed["build_systems"]:
        raise ValidationError("The selected build system has no inspected manifest")
    build = root / "build" / "ev-agent"
    if build.resolve().is_relative_to(root) is False:
        raise ValidationError("Build directory escapes the selected project")
    commands = {
        ("cmake", "configure"): [
            _platform_executable("/usr/bin/cmake"),
            "-S",
            str(root),
            "-B",
            str(build),
        ],
        ("cmake", "build"): [
            _platform_executable("/usr/bin/cmake"),
            "--build",
            str(build),
            "--parallel",
            "2",
        ],
        ("cmake", "test"): [
            _platform_executable("/usr/bin/ctest"),
            "--test-dir",
            str(build),
            "--output-on-failure",
        ],
        ("make", "build"): [_platform_executable("/usr/bin/make"), "-j2"],
        ("make", "test"): [_platform_executable("/usr/bin/make"), "test"],
        ("cargo", "build"): [_platform_executable("/usr/bin/cargo"), "build", "--offline", "-j2"],
        ("cargo", "test"): [_platform_executable("/usr/bin/cargo"), "test", "--offline", "-j2"],
        ("npm", "build"): [_platform_executable("/usr/bin/npm"), "run", "build", "--offline"],
        ("npm", "test"): [_platform_executable("/usr/bin/npm"), "test", "--offline"],
        ("python", "test"): [
            _platform_executable("/usr/bin/python3"),
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests" if (root / "tests").is_dir() else ".",
        ],
    }
    if operation == "run":
        executable = Path(a.get("executable", "")).expanduser()
        if (
            not executable.is_absolute()
            or executable.is_symlink()
            or not executable.resolve().is_relative_to(root)
            or not executable.is_file()
            or not os.access(executable, os.X_OK)
        ):
            raise ValidationError(
                "Run requires an exact executable regular file inside the selected project"
            )
        command = [str(executable.resolve()), *a.get("arguments", [])]
    else:
        if a.get("executable") or a.get("arguments"):
            raise ValidationError("Executable/arguments are only accepted for the run operation")
        command = commands.get((system, operation))
        if command is None or not Path(command[0]).is_file():
            raise ValidationError("That operation/toolchain is not available; no command was run")
    if not Path(_platform_executable("/usr/bin/bwrap")).is_file():
        raise ValidationError(
            "Bubblewrap is required; unsandboxed project execution is not a fallback"
        )
    if (root / ".env").exists():
        # A project can contain secrets even if parent home files are hidden.
        # Require an explicit sanitized workspace instead of silently exposing it.
        raise ValidationError(
            "This project has a .env file; use a sanitized build workspace without secrets"
        )
    validate_mount_tree(root)
    sandbox = [
        _platform_executable("/usr/bin/bwrap"),
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--ro-bind",
        "/usr",
        "/usr",
        "--symlink",
        "usr/bin",
        "/bin",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--dir",
        "/tmp/ev-home",
        "--bind",
        str(root),
        str(root),
        "--chdir",
        str(root),
        "--clearenv",
        "--setenv",
        "HOME",
        "/tmp/ev-home",
        "--setenv",
        "PATH",
        "/usr/bin:/bin",
        "--setenv",
        "LANG",
        "C.UTF-8",
        "--setenv",
        "CMAKE_BUILD_PARALLEL_LEVEL",
        "2",
        "--setenv",
        "OMP_NUM_THREADS",
        "2",
        "--setenv",
        "GIT_TERMINAL_PROMPT",
        "0",
    ]
    if Path("/etc/ld.so.cache").is_file():
        sandbox += ["--ro-bind", "/etc/ld.so.cache", "/etc/ld.so.cache"]
    return root, command, [*sandbox, "--", *command]


def diagnose_output(text, root):
    # Extract an initial primary diagnostic; retain bounded output for follow-up.
    locations = []
    pattern = re.compile(r"^(.*?):(\d+)(?::(\d+))?:\s*(?:fatal )?error:\s*(.*)$")
    for line in text.splitlines():
        match = pattern.match(line)
        if match:
            candidate = Path(match[1])
            candidate = (candidate if candidate.is_absolute() else root / candidate).resolve()
            if candidate.is_relative_to(root):
                locations.append(
                    {
                        "path": str(candidate),
                        "line": int(match[2]),
                        "column": int(match[3] or 1),
                        "message": match[4][:500],
                    }
                )
        if len(locations) >= 5:
            break
    return {
        "primary_diagnostic": locations[0] if locations else None,
        "additional_diagnostics": locations[1:],
        "root_cause_verified": False,
        "note": "First compiler error location, not proof of the underlying root cause",
    }


def register_project_tools(registry, state_root):
    async def execute(a, c):
        root, command, sandbox = await asyncio.to_thread(project_command, a, c)
        state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        run_id = uuid.uuid4().hex
        directory = state_root / run_id
        directory.mkdir(parents=True, mode=0o700)
        log = directory / "output.log"
        descriptor = os.open(log, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        started = time.monotonic()
        process, output, limited = None, bytearray(), False

        async def stop_process():
            if process and process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()

        try:
            # Lower scheduling priority; actual CPU parallelism remains bounded
            # for the fixed build commands. This is not a cgroup resource quota.
            spawning = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    _platform_executable("/usr/bin/nice"),
                    "-n",
                    "10",
                    *sandbox,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                    env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                )
            )
            try:
                process = await asyncio.shield(spawning)
            except asyncio.CancelledError:
                # Cancellation during creation must not orphan the process
                # before we obtain its exact PID for the finally cleanup.
                process = await spawning
                raise

            async def collect():
                nonlocal limited
                while chunk := await process.stdout.read(4096):
                    if len(output) + len(chunk) > 262144:
                        limited = True
                        output.extend(chunk[: 262144 - len(output)])
                        await stop_process()
                        break
                    output.extend(chunk)
                await process.wait()

            timed_out = False
            try:
                await asyncio.wait_for(collect(), a.get("timeout_seconds", 90))
            except TimeoutError:
                timed_out = True
                await stop_process()
            text = output.decode("utf-8", errors="replace")
            ok = process.returncode == 0 and not timed_out and not limited
            result = {
                "ok": ok,
                "run_id": run_id,
                "project": str(root),
                "command": command,
                "exit_code": process.returncode,
                "timed_out": timed_out,
                "output_limited": limited,
                "log_path": str(log),
                "output": text[-8000:],
                "output_is_untrusted": True,
                "verified": ok,
                "verification_scope": "Process exit status, not artifact quality or overall user goal",
                "sandbox": "Bubblewrap; selected project writable, network/desktop/home outside project unavailable",
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
                **diagnose_output(text, root),
                **(
                    {}
                    if ok
                    else {
                        "error": "Project command failed, timed out, or exceeded output budget; inspect its diagnostic log"
                    }
                ),
            }
            _write_private(
                directory / "result.json", json.dumps(result, ensure_ascii=False).encode("utf-8")
            )
            return result
        finally:
            await asyncio.shield(stop_process())
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(output)

    path = {"type": "string", "minLength": 1, "maxLength": 4096}

    def read_result(a, c):
        directory = state_root / a["run_id"]
        if directory.is_symlink():
            raise ValidationError("Run directory identity changed")
        try:
            data, _ = _snapshot(directory / "result.json", 65536)
            record = json.loads(data)
        except (OSError, ValueError) as error:
            raise ValidationError(
                "No complete saved result for that run; it may have been interrupted"
            ) from error
        if not isinstance(record, dict) or record.get("run_id") != a["run_id"]:
            raise ValidationError("Saved run identity mismatch")
        return {
            "run": record,
            "historical": True,
            "content_is_untrusted": True,
            "scope": "Saved process evidence only; source and artifacts may have changed since this run",
        }

    registry.register(
        ToolSpec(
            "development.project.result",
            "DEVELOPMENT",
            "Read bounded saved evidence for one exact previous project run ID, including compiler file/line/column. Historical output is untrusted and not a current source-state observation. Never reruns a command.",
            Permission.SAFE,
            object_schema({"run_id": {"type": "string", "pattern": "[a-f0-9]{32}"}}, ["run_id"]),
            read_result,
            read_only=True,
        )
    )
    registry.register(
        ToolSpec(
            "development.project.inspect",
            "DEVELOPMENT",
            "Inspect one project root's supported build manifests without executing code. Return manifest revision, detected build systems and sandbox availability. No credentials or file contents returned.",
            Permission.SAFE,
            object_schema({"project": path}, ["project"]),
            inspect_project,
            read_only=True,
        )
    )
    registry.register(
        ToolSpec(
            "development.project.run",
            "DEVELOPMENT",
            "Run an explicitly approved configure/build/test or exact project executable inside Bubblewrap. Only the selected project is writable; no desktop bus, other home files or network. Requires the freshly inspected manifest hash. Offline dependencies must already be available. Project code may alter files within the project; this is not a source-edit repair tool. Returns bounded untrusted output, a private log and first compiler error location.",
            Permission.HIGH,
            object_schema(
                {
                    "project": path,
                    "build_system": {"type": "string", "enum": list(MARKERS)},
                    "operation": {"type": "string", "enum": ["configure", "build", "test", "run"]},
                    "expected_manifest_sha256": {"type": "string", "pattern": "[a-f0-9]{64}"},
                    "executable": path,
                    "arguments": {
                        "type": "array",
                        "maxItems": 20,
                        "items": {"type": "string", "maxLength": 500},
                    },
                    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120},
                },
                ["project", "build_system", "operation", "expected_manifest_sha256"],
            ),
            execute,
            timeout_seconds=130,
            requires_confirmation=True,
            confirmation_reason="Execute project-controlled code in a network-isolated sandbox. The selected project is writable; its build scripts may modify its files.",
            verification="Process return code plus bounded diagnostic capture; not overall goal verification",
        )
    )
