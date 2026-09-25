from ev.platform import executable as _platform_executable

"""Native settings with explicit availability and exact readback; no GUI fallback."""
import json
import re
import time

from ..permissions import Permission
from .base import ToolSpec
from .builtin import object_schema, desktop_entries
from .settings import checked, _SETTINGS_LOCK

_PROFILE = [
    _platform_executable("/usr/bin/qdbus6"),
    "org.kde.Solid.PowerManagement",
    "/org/kde/Solid/PowerManagement/Actions/PowerProfile",
]
_INTERFACE = "org.kde.Solid.PowerManagement.Actions.PowerProfile."


def power_profile_status(a, c):
    try:
        captured = time.monotonic()
        choices = checked(_PROFILE + [_INTERFACE + "profileChoices"]).splitlines()
        current = checked(_PROFILE + [_INTERFACE + "currentProfile"])
        choices = [v.strip() for v in choices if re.fullmatch(r"[a-z][a-z0-9-]{0,63}", v.strip())]
        available = bool(choices and current in choices)
        return {
            "available": available,
            "profiles": choices,
            "current": current,
            "captured_at_monotonic": captured,
            "backend": "PowerDevil PowerProfile D-Bus",
            "reason": (
                "Native profile state observed"
                if available
                else "PowerDevil exposes no usable profile backend"
            ),
        }
    except (RuntimeError, OSError) as error:
        return {"available": False, "profiles": [], "current": None, "reason": str(error)}


def power_profile_set(a, c):
    with _SETTINGS_LOCK:
        before = power_profile_status({}, c)
        if not before["available"] or a["profile"] not in before["profiles"]:
            raise ValueError("Choose an available native profile; no system change was made")
        if before["current"] == a["profile"]:
            return {**before, "verified": True, "already_set": True}
        checked(_PROFILE + [_INTERFACE + "setProfile", a["profile"]])
        after = power_profile_status({}, c)
        return {
            **after,
            "verified": after["available"] and after["current"] == a["profile"],
            "previous": before["current"],
        }


def display_status(a, c):
    data = json.loads(checked([_platform_executable("/usr/bin/kscreen-doctor"), "--json"], 4))
    if not isinstance(data.get("outputs"), list):
        raise RuntimeError("KScreen did not return a valid output list")
    outputs = []
    for output in data["outputs"][:16]:
        modes = [
            {k: mode.get(k) for k in ("id", "name", "refreshRate", "size")}
            for mode in output.get("modes", [])[:120]
        ]
        outputs.append(
            {
                **{
                    k: output.get(k)
                    for k in (
                        "id",
                        "name",
                        "connected",
                        "enabled",
                        "currentModeId",
                        "pos",
                        "size",
                        "scale",
                        "rotation",
                        "priority",
                    )
                },
                "modes": modes,
                "modes_limited": len(output.get("modes", [])) > 120,
            }
        )
    return {
        "outputs": outputs,
        "outputs_limited": len(data["outputs"]) > 16,
        "backend": "KScreen",
        "observed": True,
        "changed": False,
        "scope": "Native monitor topology and supported modes; no change applied",
    }


def night_light_status(a, c):
    captured = time.monotonic()
    base = [_platform_executable("/usr/bin/qdbus6"), "org.kde.KWin", "/org/kde/KWin/NightLight"]
    result = {"backend": "KWin NightLight", "changed": False}
    for name in (
        "available",
        "enabled",
        "running",
        "inhibited",
        "currentTemperature",
        "targetTemperature",
    ):
        raw = checked(
            base + ["org.freedesktop.DBus.Properties.Get", "org.kde.KWin.NightLight", name]
        )
        if name.endswith("Temperature"):
            result[name] = int(raw)
        elif raw.casefold() in {"true", "false"}:
            result[name] = raw.casefold() == "true"
        else:
            raise RuntimeError("Unexpected NightLight property value")
    return {**result, "captured_at_monotonic": captured}


def association_get(a, c):
    captured = time.monotonic()
    current = checked(
        [_platform_executable("/usr/bin/xdg-mime"), "query", "default", a["mime_type"]]
    )
    return {
        "mime_type": a["mime_type"],
        "desktop_id": current or None,
        "backend": "xdg-mime",
        "captured_at_monotonic": captured,
    }


def association_set(a, c):
    with _SETTINGS_LOCK:
        desktop_id = a["desktop_id"].removesuffix(".desktop")
        if desktop_id not in desktop_entries():
            raise ValueError(
                "Choose an exact installed desktop application; no handler was changed"
            )
        before = association_get(a, c)
        if (before["desktop_id"] or "") != a["expected_current"]:
            raise ValueError("Default application changed since inspection; read it again")
        target = desktop_id + ".desktop"
        if before["desktop_id"] == target:
            return {**before, "verified": True, "already_set": True}
        checked([_platform_executable("/usr/bin/xdg-mime"), "default", target, a["mime_type"]])
        after = association_get(a, c)
        return {
            **after,
            "verified": after["desktop_id"] == target,
            "previous": before["desktop_id"],
        }


def register_native_settings_tools(registry):
    registry.register(
        ToolSpec(
            "settings.power_profile.get",
            "SETTINGS",
            "Read native PowerDevil power profiles and current selection. An installed service with no choices is explicitly unavailable.",
            Permission.SAFE,
            object_schema({}, []),
            power_profile_status,
            read_only=True,
        )
    )
    registry.register(
        ToolSpec(
            "settings.power_profile.set",
            "SETTINGS",
            "Select one profile reported by settings.power_profile.get and read back the actual current profile. No CPU clock/thermal overrides or root commands.",
            Permission.LOW_RISK,
            object_schema(
                {"profile": {"type": "string", "pattern": "[a-z][a-z0-9-]{0,63}"}}, ["profile"]
            ),
            power_profile_set,
        )
    )
    registry.register(
        ToolSpec(
            "settings.displays.inspect",
            "SETTINGS",
            "Read actual monitor arrangement, resolution, refresh-rate modes, scaling and rotation from KScreen. Does not change the layout or turn screens off.",
            Permission.SAFE,
            object_schema({}, []),
            display_status,
            read_only=True,
        )
    )
    registry.register(
        ToolSpec(
            "settings.night_light.inspect",
            "SETTINGS",
            "Read actual KWin Night Light availability, enabled/running/inhibited state and temperatures. Does not alter appearance.",
            Permission.SAFE,
            object_schema({}, []),
            night_light_status,
            read_only=True,
        )
    )
    mime = {"type": "string", "pattern": "[a-zA-Z0-9.+_-]+/[a-zA-Z0-9.+_-]+", "maxLength": 150}
    registry.register(
        ToolSpec(
            "settings.default_application.get",
            "SETTINGS",
            "Inspect the current desktop-file handler for one exact MIME type, such as application/pdf or x-scheme-handler/https.",
            Permission.SAFE,
            object_schema({"mime_type": mime}, ["mime_type"]),
            association_get,
            read_only=True,
        )
    )
    registry.register(
        ToolSpec(
            "settings.default_application.set",
            "SETTINGS",
            "Set a MIME handler to an exact installed desktop app, only if expected_current still matches the observed handler (empty if none); verify readback. Does not launch it.",
            Permission.SENSITIVE,
            object_schema(
                {
                    "mime_type": mime,
                    "desktop_id": {
                        "type": "string",
                        "pattern": "[a-zA-Z0-9._-]+",
                        "maxLength": 200,
                    },
                    "expected_current": {"type": "string", "maxLength": 200},
                },
                ["mime_type", "desktop_id", "expected_current"],
            ),
            association_set,
        )
    )
