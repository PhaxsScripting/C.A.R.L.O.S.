"""Supported desktop settings, exact targets and read-back verification."""

from __future__ import annotations

from ev.platform import executable as _platform_executable

import json
import re
import subprocess
import threading
import time

from .base import ToolRegistry, ToolSpec
from .builtin import object_schema, run_command
from ..permissions import Permission

PAGES = {
    "display": "kcm_kscreen",
    "audio": "kcm_pulseaudio",
    "bluetooth": "kcm_bluetooth",
    "network": "kcm_networkmanagement",
    "wifi": "kcm_networkmanagement",
    "power": "kcm_powerdevilprofilesconfig",
    "keyboard": "kcm_keyboard",
    "shortcuts": "kcm_keys",
    "mouse": "kcm_mouse",
    "touchpad": "kcm_touchpad",
    "notifications": "kcm_notifications",
    "accessibility": "kcm_access",
    "default apps": "kcm_componentchooser",
    "night light": "kcm_nightlight",
    "screen lock": "kcm_screenlocker",
    "date and time": "kcm_clock",
    "language": "kcm_regionandlang",
    "printers": "kcm_printer_manager",
    "users": "kcm_users",
    "wallpaper": "kcm_wallpaper",
    "colors": "kcm_colors",
    "fonts": "kcm_fonts",
    "icons": "kcm_icons",
    "startup": "kcm_autostart",
    "virtual desktops": "kcm_kwin_virtualdesktops",
    "about": "kcm_about-distro",
}
_SETTINGS_LOCK = threading.RLock()
_BRIGHTNESS = [
    _platform_executable("/usr/bin/qdbus6"),
    "org.kde.Solid.PowerManagement",
    "/org/kde/Solid/PowerManagement/Actions/BrightnessControl",
]
_BRIGHTNESS_INTERFACE = "org.kde.Solid.PowerManagement.Actions.BrightnessControl."


def checked(command, timeout=3):
    result = run_command(command, timeout=timeout)
    if not result["ok"]:
        raise RuntimeError(
            "The native settings service rejected the request or is unavailable. Open its settings page for details."
        )
    return result["stdout"].strip()


def open_page(a, c):
    page = a["page"]
    module = PAGES[page]
    available = checked([_platform_executable("/usr/bin/kcmshell6"), "--list"], 5)
    if not re.search(r"^" + re.escape(module) + r"\s", available, re.M):
        raise ValueError(f"The {page} settings page is not installed")
    subprocess.Popen(
        [_platform_executable("/usr/bin/systemsettings"), module],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return {
        "requested": True,
        "verified": False,
        "message": f"Requested the {page} settings page. No setting was changed.",
    }


def brightness_get(a, c):
    with _SETTINGS_LOCK:
        captured = time.monotonic()
        values = [
            int(checked(_BRIGHTNESS + [_BRIGHTNESS_INTERFACE + method], 2))
            for method in ("brightness", "brightnessMax", "knownSafeBrightnessMin")
        ]
    current, maximum, minimum = values
    if maximum <= 0 or not 0 <= current <= maximum or not 0 <= minimum <= maximum:
        raise ValueError("The native brightness device reported invalid limits")
    return {
        "current": current,
        "maximum": maximum,
        "safe_minimum": minimum,
        "captured_at_monotonic": captured,
        "percent": round(current * 100 / maximum),
        "scope": "PowerDevil's managed brightness control",
        "message": f"Managed screen brightness is {round(current * 100 / maximum)}%. External monitors may use separate controls.",
    }


def brightness_change(a, c):
    with _SETTINGS_LOCK:
        before = brightness_get({}, c)
        percent = a.get("percent", max(5, min(100, before["percent"] + a.get("delta", 0))))
        if not 5 <= percent <= 100:
            raise ValueError("Brightness changes are limited to 5–100% so the screen stays visible")
        target = max(1, before["safe_minimum"], round(percent * before["maximum"] / 100))
        checked(_BRIGHTNESS + [_BRIGHTNESS_INTERFACE + "setBrightness", str(target)])
        after = brightness_get({}, c)
        verified = after["maximum"] == before["maximum"] and abs(after["current"] - target) <= max(
            1, round(before["maximum"] * 0.01)
        )
        return {
            **after,
            "verified": verified,
            "previous_percent": before["percent"],
            "message": (
                f"Managed brightness is now {after['percent']}%."
                if verified
                else "The brightness change did not verify; no automatic retry was made."
            ),
        }


def radios_status(a, c):
    result = {"wifi_available": False, "bluetooth_available": False}
    try:
        wifi = checked([_platform_executable("/usr/bin/nmcli"), "radio", "wifi"])
        if wifi in {"enabled", "disabled"}:
            result.update(wifi_available=True, wifi_enabled=wifi == "enabled")
    except (OSError, RuntimeError):
        pass
    try:
        controller = checked([_platform_executable("/usr/bin/bluetoothctl"), "show"])
        match = re.search(r"^\s*Powered: (yes|no)\s*$", controller, re.M)
        if match:
            result.update(bluetooth_available=True, bluetooth_enabled=match[1] == "yes")
    except (OSError, RuntimeError):
        pass
    result["message"] = (
        "; ".join(
            (
                label + ": " + ("on" if result.get(key + "_enabled") else "off")
                if result[key + "_available"]
                else label + ": unavailable"
            )
            for key, label in (("wifi", "Wi-Fi"), ("bluetooth", "Bluetooth"))
        )
        + "."
    )
    return result


def radio_set(a, c):
    radio, enabled = a["radio"], a["enabled"]
    with _SETTINGS_LOCK:
        if radio == "wifi":
            command = [
                _platform_executable("/usr/bin/nmcli"),
                "radio",
                "wifi",
                "on" if enabled else "off",
            ]
        else:
            controllers = checked([_platform_executable("/usr/bin/bluetoothctl"), "list"])
            if len(re.findall(r"^Controller ", controllers, re.M)) != 1:
                raise ValueError("Bluetooth power changes require one unambiguous controller")
            command = [
                _platform_executable("/usr/bin/bluetoothctl"),
                "power",
                "on" if enabled else "off",
            ]
        checked(command, 5)
        after = radios_status({}, c)
        verified = after.get(radio + "_available") and after.get(radio + "_enabled") is enabled
        return {
            "verified": bool(verified),
            "message": (
                f"{radio.title()} is {'on' if enabled else 'off'}."
                if verified
                else "Radio change could not be verified. Check the native settings page."
            ),
        }


def streams_list(a, c):
    streams = json.loads(
        checked([_platform_executable("/usr/bin/pactl"), "--format=json", "list", "sink-inputs"])
    )
    items = []
    for stream in streams:
        properties = stream.get("properties", {})
        name = properties.get("application.name", "")
        if name == "E.V.":
            continue
        items.append(
            {
                "id": str(stream["index"]),
                "title": name or "Unnamed audio stream",
                "muted": stream.get("mute"),
                "percent": round(
                    max(
                        (channel["value"] for channel in stream.get("volume", {}).values()),
                        default=0,
                    )
                    * 100
                    / 65536
                ),
            }
        )
    return {
        "streams": items,
        "message": "; ".join(
            f"{item['title']}: {item['percent']}%" + (" muted" if item["muted"] else "")
            for item in items
        )
        or "No application audio streams are active.",
    }


def _named_devices(c):
    from .builtin import get_audio_devices

    result = get_audio_devices({}, c)
    names = {}
    for item in result["inputs"] + result["outputs"]:
        # Some PulseAudio builds return '(null)' for Unicode Bluetooth labels.
        # Ask BlueZ for the alias of that exact address; never assume the only
        # Bluetooth device is the user's intended headphones.
        if item["description"] and item["description"] != "(null)":
            continue
        match = re.match(r"bluez_(?:input|output)\.([0-9A-Fa-f:_]{17})(?:\.|$)", item["name"])
        address = match[1].replace("_", ":") if match else ""
        if address and address not in names:
            try:
                info = checked([_platform_executable("/usr/bin/bluetoothctl"), "info", address])
                alias = re.search(r"^\s*Alias:\s*(.+)$", info, re.M)
                names[address] = alias[1].strip() if alias else ""
            except (OSError, RuntimeError):
                names[address] = ""
        item["description"] = names.get(address) or item["name"]
    return result


def devices_list(a, c):
    result = _named_devices(c)
    descriptions = []
    for direction, key in (("input", "inputs"), ("output", "outputs")):
        names = [
            (item["description"] or item["name"])
            + (" (default)" if item["name"] == result["default_" + direction] else "")
            for item in result[key]
        ]
        descriptions.append(direction.title() + "s: " + (", ".join(names) or "none"))
    return {**result, "message": "; ".join(descriptions) + "."}


def device_select(a, c):
    from .builtin import get_audio_devices, set_default_audio_input, set_default_audio_output

    direction, query = a["direction"], a["name"].strip().casefold()
    devices = _named_devices(c)["inputs" if direction == "input" else "outputs"]
    exact = [d for d in devices if query in {d["name"].casefold(), d["description"].casefold()}]
    matches = exact or [
        d
        for d in devices
        if query and query in (d["description"] + " " + d.get("device_description", "")).casefold()
    ]
    if len(matches) != 1:
        raise ValueError(
            "Name one unambiguous audio device from 'show audio devices'. No default was changed."
        )
    item = matches[0]
    result = (
        set_default_audio_input({"source": item["name"]}, c)
        if direction == "input"
        else set_default_audio_output({"sink": item["name"]}, c)
    )
    return {
        **result,
        "message": (
            f"Default {direction} is now {item['description'] or item['name']}."
            if result.get("verified")
            else "The audio device change did not verify."
        ),
    }


def settings_overview(a, c):
    result = {}
    for name, function in (
        ("brightness", brightness_get),
        ("radios", radios_status),
        ("audio", devices_list),
    ):
        try:
            result[name] = function({}, c)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            result[name] = {
                "available": False,
                "message": name.title() + " settings are unavailable through the native service.",
            }
    result["message"] = " ".join(value["message"] for value in result.values())
    return result


def stream_set(a, c):
    with _SETTINGS_LOCK:

        def listing():
            return json.loads(
                checked(
                    [_platform_executable("/usr/bin/pactl"), "--format=json", "list", "sink-inputs"]
                )
            )

        def identity(s):
            p = s.get("properties", {})
            return (
                s.get("index"),
                s.get("client"),
                p.get("application.process.id"),
                p.get("application.name"),
                p.get("media.name"),
            )

        target = a["application"].casefold().strip()
        aliases = {
            "firefox": {"firefox", "firefox audio"},
            "spotify": {"spotify"},
            "discord": {"discord"},
        }
        accepted = aliases.get(target, {target})
        streams = [
            s
            for s in listing()
            if str(s.get("properties", {}).get("application.name", "")).casefold() in accepted
        ]
        if len(streams) != 1 or target in {"e.v.", "ev"}:
            raise ValueError(
                "Specify one application with exactly one active audio stream. No global volume was changed."
            )
        selected = streams[0]
        exact = identity(selected)
        # Recheck identity immediately before sending a fixed, numeric-target
        # command. A vanished/replaced stream is not permission to change another.
        if not any(identity(s) == exact for s in listing()):
            raise ValueError("The selected application audio stream changed")
        index = str(selected["index"])
        if not index.isdigit():
            raise ValueError("Invalid audio stream ID")
        if "percent" in a:
            checked(
                [
                    _platform_executable("/usr/bin/pactl"),
                    "set-sink-input-volume",
                    index,
                    f"{a['percent']}%",
                ]
            )
        else:
            checked(
                [
                    _platform_executable("/usr/bin/pactl"),
                    "set-sink-input-mute",
                    index,
                    "1" if a["muted"] else "0",
                ]
            )
        after = next((s for s in listing() if identity(s) == exact), None)
        if "percent" in a:
            volumes = [v["value"] * 100 / 65536 for v in (after or {}).get("volume", {}).values()]
            verified = bool(volumes) and all(abs(v - a["percent"]) <= 1 for v in volumes)
        else:
            verified = after is not None and after.get("mute") is a["muted"]
        return {
            "verified": verified,
            "stream_index": index,
            "stream_identity": [str(value or "") for value in exact[1:]],
            "message": (
                f"Updated {a['application']}'s audio and verified it."
                if verified
                else "The application audio change did not verify."
            ),
        }


def register_settings_tools(registry: ToolRegistry):
    def reg(name, description, schema, executor, permission=Permission.LOW_RISK):
        registry.register(
            ToolSpec(
                name,
                "SETTINGS",
                description,
                permission,
                schema,
                executor,
                cancellable=False,
                timeout_seconds=20,
            )
        )

    reg(
        "settings.overview",
        "Read the supported current brightness, radio and audio-device settings. Never changes configuration.",
        object_schema({}),
        settings_overview,
        Permission.SAFE,
    )
    reg(
        "settings.open",
        "Open one installed native KDE settings page. Does not change its settings or bypass OS authentication.",
        object_schema({"page": {"type": "string", "enum": list(PAGES)}}, ["page"]),
        open_page,
    )
    reg(
        "settings.brightness.get",
        "Read native PowerDevil brightness and safe device limits. External displays may have separate controls.",
        object_schema({}),
        brightness_get,
        Permission.SAFE,
    )
    reg(
        "settings.brightness.set",
        "Set managed display brightness to 5-100%, preserve the device safe minimum, and read back the actual value.",
        object_schema({"percent": {"type": "integer", "minimum": 5, "maximum": 100}}, ["percent"]),
        brightness_change,
    )
    reg(
        "settings.brightness.adjust",
        "Adjust managed brightness by a relative percentage, clamp to a visible safe range, and verify.",
        object_schema({"delta": {"type": "integer", "minimum": -100, "maximum": 100}}, ["delta"]),
        brightness_change,
    )
    reg(
        "settings.radios.status",
        "Read Wi-Fi and Bluetooth power state without scanning or changing connections.",
        object_schema({}),
        radios_status,
        Permission.SAFE,
    )
    reg(
        "settings.radio.set",
        "Set Wi-Fi or one unambiguous Bluetooth controller on/off. Turning off may disconnect network, headphones or input devices. Uses native authorization.",
        object_schema(
            {
                "radio": {"type": "string", "enum": ["wifi", "bluetooth"]},
                "enabled": {"type": "boolean"},
            },
            ["radio", "enabled"],
        ),
        radio_set,
        Permission.SENSITIVE,
    )
    reg(
        "settings.audio.apps",
        "List active application audio streams and actual volume/mute states without changing them.",
        object_schema({}),
        streams_list,
        Permission.SAFE,
    )
    name = {"type": "string", "minLength": 1, "maxLength": 160}
    reg(
        "settings.audio.devices",
        "List actual microphone/output device descriptions and defaults without changing audio profiles.",
        object_schema({}),
        devices_list,
        Permission.SAFE,
    )
    reg(
        "settings.audio.select",
        "Select one unique available named audio input/output as default and verify. Does not switch Bluetooth profiles or infer device identity.",
        object_schema(
            {"direction": {"type": "string", "enum": ["input", "output"]}, "name": name},
            ["direction", "name"],
        ),
        device_select,
    )
    reg(
        "settings.audio.app_volume",
        "Set volume for one exact active application stream; never fall back to global output or another app.",
        object_schema(
            {"application": name, "percent": {"type": "integer", "minimum": 0, "maximum": 100}},
            ["application", "percent"],
        ),
        stream_set,
    )
    reg(
        "settings.audio.app_mute",
        "Mute/unmute one exact active application stream and verify the same identity.",
        object_schema(
            {"application": name, "muted": {"type": "boolean"}}, ["application", "muted"]
        ),
        stream_set,
    )
