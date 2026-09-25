"""Bounded control of an already-running, same-user HoloHand instance."""

import asyncio
import os
import re
import socket
import stat
import struct
from pathlib import Path

from .base import ToolSpec, ValidationError
from .builtin import object_schema
from ..permissions import Permission


def socket_path():
    return (
        Path(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/holohand-{os.getuid()}") / "holohand.sock"
    )


async def exchange(command):
    if command not in {"--status", "--pause", "--resume"}:
        raise ValidationError("Unsupported HoloHand command")
    path = socket_path()
    parent = path.parent.lstat()
    node = path.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.getuid()
        or parent.st_mode & 0o077
        or not stat.S_ISSOCK(node.st_mode)
        or node.st_uid != os.getuid()
    ):
        raise ValidationError("HoloHand socket ownership or permissions are unsafe")
    reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(path), 1)
    try:
        peer = writer.get_extra_info("socket")
        if hasattr(socket, "SO_PEERCRED"):
            _, uid, _ = struct.unpack(
                "3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )
            if uid != os.getuid():
                raise ValidationError("HoloHand peer belongs to another user")
        writer.write(command.encode())
        await writer.drain()

        async def receive():
            data = bytearray()
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    return bytes(data)
                data.extend(chunk)
                if len(data) > 8192:
                    raise ValidationError("HoloHand status exceeds the protocol limit")

        data = await asyncio.wait_for(receive(), 2)
        text = data.decode("utf-8", errors="strict")
        state = text.split("|", 1)[0].strip()
        if state not in {"READY", "PAUSED", "CALIBRATION REQUIRED"}:
            raise ValidationError("Unrecognized HoloHand status")
        match = re.search(
            r"Hand: (visible|not detected); confidence ([0-9.]+); inference ([0-9.]+) ms; age ([0-9.]+) ms;",
            text,
        )
        tracking = {}
        if match:
            try:
                confidence, inference, age = map(float, match.groups()[1:])
                if 0 <= confidence <= 1 and 0 <= inference <= 60000 and 0 <= age <= 60000:
                    tracking = {
                        "hand_visible": match[1] == "visible",
                        "confidence": confidence,
                        "inference_ms": inference,
                        "age_ms": age,
                    }
            except ValueError:
                pass
        return {
            "available": True,
            "state": state,
            "details": text,
            "tracking": tracking,
            "tracking_quality": "UNVERIFIED",
            "camera_started": False,
        }
    finally:
        writer.close()
        await writer.wait_closed()


async def status(arguments, context):
    try:
        return await exchange("--status")
    except (OSError, TimeoutError):
        return {"available": False, "state": "UNAVAILABLE", "camera_started": False}


async def set_paused(arguments, context):
    desired = arguments["paused"]
    # No launch fallback: resuming here only controls an existing instance.
    await exchange("--pause" if desired else "--resume")
    observed = await exchange("--status")
    observed["verified"] = (
        observed["state"] == "PAUSED" if desired else observed["state"] == "READY"
    )
    observed["requested_paused"] = desired
    return observed


def register_holohand_tools(registry):
    registry.register(
        ToolSpec(
            "holohand.status",
            "HOLOHAND",
            "Read the running HoloHand gesture controller. Does not start a camera or launch the app. Tracking quality remains unverified.",
            Permission.SAFE,
            object_schema({}, []),
            status,
            read_only=True,
            offline_available=True,
            reversible=True,
            timeout_seconds=4,
        )
    )
    registry.register(
        ToolSpec(
            "holohand.set_paused",
            "HOLOHAND",
            "Pause or resume an already-running HoloHand instance and read back its state. Pause releases held gesture input; resume enables gesture control.",
            Permission.LOW_RISK,
            object_schema({"paused": {"type": "boolean"}}, ["paused"]),
            set_paused,
            offline_available=True,
            reversible=True,
            timeout_seconds=7,
            side_effects=("Changes gesture input state on the running HoloHand instance",),
        )
    )
