"""Finite, read-only goal predicates. No model-written code or truth assertions.

Predicates certify declared conditions, not semantic alignment of an arbitrary
model-selected goal with the user's whole request. Unobservable facts fail closed.
"""

import time
import json
import math
import hashlib

from .tools.base import ValidationError, validate_schema


def validate_conditions(conditions):
    if not isinstance(conditions, list) or not 1 <= len(conditions) <= 12:
        raise ValidationError("Supply 1-12 concrete goal conditions")
    fields = {
        "window_exists": {"window_id"},
        "window_absent": {"window_id"},
        "window_active": {"window_id"},
        "window_state": {"window_id", "property", "expected"},
        "window_geometry": {"window_id", "geometry"},
        "file_hash": {"path", "sha256"},
        "file_kind": {"path", "expected"},
        "file_text": {"path", "expected"},
        "audio_volume": {"expected"},
        "audio_muted": {"expected"},
        "control_state": {"target", "property", "expected"},
        "control_text": {"target", "expected"},
        "process_ended": {"pid", "start_ticks", "boot_id"},
        "browser_url": {"window_id", "expected"},
        "media_playback": {"service", "owner", "expected"},
        "power_profile": {"expected"},
        "screen_brightness": {"expected"},
        "default_application": {"mime_type", "expected"},
        "night_light_state": {"property", "expected"},
    }
    for condition in conditions:
        if not isinstance(condition, dict) or condition.get("kind") not in fields:
            raise ValidationError("Unsupported goal condition")
        kind = condition["kind"]
        if set(condition) != fields[kind] | {"kind"}:
            raise ValidationError("Goal condition has missing or unknown fields")
        if kind == "power_profile":
            validate_schema(
                condition["expected"], {"type": "string", "pattern": "^[a-z][a-z0-9-]{0,63}$"}
            )
        if kind == "screen_brightness":
            validate_schema(
                condition["expected"], {"type": "integer", "minimum": 0, "maximum": 100}
            )
        if kind == "default_application":
            validate_schema(
                condition["mime_type"],
                {
                    "type": "string",
                    "maxLength": 150,
                    "pattern": "^[a-zA-Z0-9.+_-]+/[a-zA-Z0-9.+_-]+$",
                },
            )
            validate_schema(
                condition["expected"],
                {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "pattern": "^[a-zA-Z0-9._-]+\\.desktop$",
                },
            )
        if kind == "night_light_state":
            validate_schema(
                condition["property"],
                {"type": "string", "enum": ["enabled", "running", "inhibited"]},
            )
            validate_schema(condition["expected"], {"type": "boolean"})
        if kind == "media_playback":
            import re

            if (
                not isinstance(condition["service"], str)
                or len(condition["service"]) > 200
                or not re.fullmatch(
                    r"org\.mpris\.MediaPlayer2\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*",
                    condition["service"],
                )
                or not isinstance(condition["owner"], str)
                or not re.fullmatch(r":[0-9]{1,12}\.[0-9]{1,12}", condition["owner"])
                or not isinstance(condition["expected"], str)
                or condition["expected"] not in {"Playing", "Paused", "Stopped"}
            ):
                raise ValidationError(
                    "Media condition needs an exact service, unique owner and playback state"
                )
        if kind == "process_ended":
            validate_schema(
                {k: condition[k] for k in ("pid", "start_ticks", "boot_id")},
                {
                    "type": "object",
                    "properties": {
                        "pid": {"type": "integer", "minimum": 1, "maximum": 4194304},
                        "start_ticks": {"type": "string", "pattern": "^[0-9]{1,24}$"},
                        "boot_id": {
                            "type": "string",
                            "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
                        },
                    },
                    "required": ["pid", "start_ticks", "boot_id"],
                    "additionalProperties": False,
                },
            )
        if kind == "browser_url":
            from urllib.parse import urlsplit

            expected = condition["expected"]
            if not isinstance(expected, str) or not 1 <= len(expected) <= 2000:
                raise ValidationError("Browser URL condition needs an exact HTTP(S) URL")
            parsed = urlsplit(expected)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValidationError("Unsupported browser URL condition")
        for key in ("window_id", "path"):
            if key in condition and (
                not isinstance(condition[key], str) or not 1 <= len(condition[key]) <= 4096
            ):
                raise ValidationError("Goal needs an exact target identity")
        if kind == "window_state" and (
            condition["property"] not in {"minimized", "fullscreen", "maximized"}
            or not isinstance(condition["expected"], bool)
        ):
            raise ValidationError("Unsupported window-state predicate")
        if kind == "window_geometry":
            geometry = condition["geometry"]
            if (
                not isinstance(geometry, dict)
                or set(geometry) != {"x", "y", "width", "height"}
                or any(
                    type(v) not in (int, float) or not -32768 <= v <= 32768
                    for v in geometry.values()
                )
                or min(geometry["width"], geometry["height"]) <= 0
            ):
                raise ValidationError("Invalid expected window geometry")
        if kind == "file_hash":
            import re

            if not isinstance(condition["sha256"], str) or not re.fullmatch(
                "[a-f0-9]{64}", condition["sha256"]
            ):
                raise ValidationError("Goal requires an exact SHA-256")
        if kind == "file_kind" and condition["expected"] not in {"file", "directory"}:
            raise ValidationError("Expected file or directory")
        if kind == "file_text":
            expected = condition["expected"]
            if not isinstance(expected, str) or len(expected) > 65536:
                raise ValidationError(
                    "Expected UTF-8 text must be a string of at most 65536 characters"
                )
            try:
                expected.encode("utf-8")
            except UnicodeEncodeError as error:
                raise ValidationError("Expected text must be valid UTF-8") from error
        if kind == "audio_muted" and not isinstance(condition["expected"], bool):
            raise ValidationError("Expected boolean mute state")
        if kind == "audio_volume" and (
            type(condition["expected"]) not in (int, float) or not 0 <= condition["expected"] <= 100
        ):
            raise ValidationError("Expected volume must be 0-100")
        if kind in {"control_state", "control_text"}:
            from .tools.controls import CONTROL_IDENTITY_SCHEMA

            validate_schema(condition["target"], CONTROL_IDENTITY_SCHEMA)
            if kind == "control_text":
                validate_schema(condition["expected"], {"type": "string", "maxLength": 2000})
                continue
            prop, expected = condition["property"], condition["expected"]
            if prop not in {"checked", "selected", "value"}:
                raise ValidationError("Unsupported semantic control predicate")
            if prop == "value":
                if type(expected) not in (int, float) or not math.isfinite(expected):
                    raise ValidationError("Expected control value must be finite")
            elif type(expected) is not bool:
                raise ValidationError("Expected checked/selected state must be boolean")
    return conditions


async def verify_conditions(conditions, requester, correlation, *, cancelled=lambda: False):
    validate_conditions(conditions)
    receipts, observations = [], {}
    started = time.monotonic()
    for condition in conditions:
        if cancelled():
            return {"verified": False, "status": "CANCELLED", "conditions": receipts}
        kind = condition["kind"]
        name, arguments = (
            ("browser.document", {"window_id": condition["window_id"]})
            if kind == "browser_url"
            else (
                (
                    "system.process_lifetime",
                    {"pid": condition["pid"], "start_ticks": condition["start_ticks"]},
                )
                if kind == "process_ended"
                else (
                    ("desktop.controls.inspect", condition["target"])
                    if kind in {"control_state", "control_text"}
                    else (
                        ("desktop.world", {})
                        if kind.startswith("window_")
                        else (
                            ("audio.players", {"service": condition["service"]})
                            if kind == "media_playback"
                            else (
                                ("settings.power_profile.get", {})
                                if kind == "power_profile"
                                else (
                                    ("settings.brightness.get", {})
                                    if kind == "screen_brightness"
                                    else (
                                        (
                                            "settings.default_application.get",
                                            {"mime_type": condition["mime_type"]},
                                        )
                                        if kind == "default_application"
                                        else (
                                            ("settings.night_light.inspect", {})
                                            if kind == "night_light_state"
                                            else (
                                                ("audio.get_volume", {})
                                                if kind.startswith("audio_")
                                                else (
                                                    (
                                                        "files.hash"
                                                        if kind in {"file_hash", "file_text"}
                                                        else "files.info"
                                                    ),
                                                    {"path": condition["path"]},
                                                )
                                            )
                                        )
                                    )
                                )
                            )
                        )
                    )
                )
            )
        )
        key = (name, json.dumps(arguments, sort_keys=True))
        try:
            if key not in observations:
                observations[key] = await requester(
                    {"name": name, "arguments": arguments}, correlation
                )
            result = observations[key]
            data = result.get("result", {})
            if (
                result.get("status") != "completed"
                or not isinstance(data, dict)
                or data.get("truncated")
                or data.get("error")
                or data.get("ok") is False
            ):
                raise ValueError("Fresh observation failed or is incomplete")
            if kind in {
                "power_profile",
                "screen_brightness",
                "default_application",
                "night_light_state",
            }:
                captured = data.get("captured_at_monotonic")
                if type(captured) not in (int, float) or not 0 <= time.monotonic() - captured <= 2:
                    raise ValueError("Settings evidence is stale or undated")
                if kind == "power_profile":
                    actual = data.get("current")
                    matched = (
                        data.get("available") is True
                        and isinstance(data.get("profiles"), list)
                        and actual in data["profiles"]
                        and actual == condition["expected"]
                    )
                elif kind == "screen_brightness":
                    # PowerDevil-managed brightness only, not all monitors.
                    actual = data.get("percent")
                    matched = (
                        type(actual) in (int, float) and abs(actual - condition["expected"]) <= 1
                    )
                elif kind == "default_application":
                    actual = data.get("desktop_id")
                    matched = (
                        data.get("mime_type") == condition["mime_type"]
                        and actual == condition["expected"]
                    )
                else:
                    actual = data.get(condition["property"])
                    matched = (
                        data.get("available") is True
                        and type(actual) is bool
                        and actual == condition["expected"]
                    )
            elif kind == "media_playback":
                rows = data.get("players")
                if (
                    data.get("partial")
                    or not isinstance(rows, list)
                    or len(rows) != 1
                    or not isinstance(rows[0], dict)
                ):
                    raise ValueError("Exact media state is missing or incomplete")
                row = rows[0]
                captured = row.get("captured_at_monotonic")
                if type(captured) not in (int, float) or not 0 <= time.monotonic() - captured <= 2:
                    raise ValueError("Media evidence is stale or undated")
                actual = row.get("playback_status")
                matched = (
                    row.get("service") == condition["service"]
                    and row.get("owner") == condition["owner"]
                    and actual == condition["expected"]
                )
            elif kind == "browser_url":
                captured = data.get("captured_at_monotonic")
                if type(captured) not in (int, float) or not 0 <= time.monotonic() - captured <= 2:
                    raise ValueError("Browser URL evidence is stale or undated")
                actual = data.get("url")
                matched = (
                    data.get("window_id") == condition["window_id"]
                    and data.get("busy") is False
                    and actual == condition["expected"]
                )
            elif kind == "process_ended":
                actual = data.get("lifetime_status")
                matched = (
                    data.get("boot_id") == condition["boot_id"]
                    and data.get("pid") == condition["pid"]
                    and actual in {"ABSENT", "EXITED", "REPLACED"}
                )
            elif kind.startswith("window_"):
                if not isinstance(data.get("windows"), list) or data.get("windows_truncated"):
                    raise ValueError("Complete window list unavailable")
                captured = data.get("captured_at_monotonic")
                if type(captured) not in (int, float) or not 0 <= time.monotonic() - captured <= 2:
                    raise ValueError("Window evidence is stale or undated")
                matches = [w for w in data["windows"] if w.get("id") == condition["window_id"]]
                if kind == "window_absent":
                    matched, actual = not matches, len(matches)
                elif kind == "window_exists":
                    matched, actual = len(matches) == 1, len(matches)
                elif kind == "window_active":
                    actual = data.get("active_window_id")
                    matched = len(matches) == 1 and actual == condition["window_id"]
                elif kind == "window_geometry":
                    actual = matches[0].get("geometry", {}) if len(matches) == 1 else {}
                    matched = all(
                        type(actual.get(k)) in (int, float) and abs(actual[k] - v) <= 2
                        for k, v in condition["geometry"].items()
                    )
                else:
                    actual = matches[0].get(condition["property"]) if len(matches) == 1 else None
                    matched = type(actual) is bool and actual == condition["expected"]
            elif kind == "control_text":
                actual = (
                    data.get("text")
                    if data.get("text_available") is True and not data.get("text_truncated")
                    else None
                )
                matched = isinstance(actual, str) and actual == condition["expected"]
            elif kind == "control_state":
                prop = condition["property"]
                if prop == "value":
                    actual = data.get("value") if data.get("value_available") else None
                    matched = (
                        type(actual) in (int, float)
                        and abs(actual - condition["expected"]) <= 0.000001
                    )
                else:
                    actual = data.get("element", {}).get("states", {}).get(prop)
                    matched = type(actual) is bool and actual == condition["expected"]
            elif kind in {"file_hash", "file_text"}:
                actual = data.get("sha256")
                # Compute expected bytes locally. A language model must not
                # invent cryptographic digests to verify user-specified text.
                expected_hash = (
                    hashlib.sha256(condition["expected"].encode("utf-8")).hexdigest()
                    if kind == "file_text"
                    else condition["sha256"]
                )
                matched = actual == expected_hash
            elif kind == "file_kind":
                actual = data.get("is_file" if condition["expected"] == "file" else "is_directory")
                matched = actual is True
            elif kind == "audio_muted":
                actual = data.get("muted")
                matched = type(actual) is bool and actual == condition["expected"]
            else:
                actual = data.get("percent")
                matched = type(actual) in (int, float) and abs(actual - condition["expected"]) <= 1
            receipts.append(
                {"condition": condition, "verified": matched, "actual": actual, "source": name}
            )
        except Exception as error:
            receipts.append(
                {"condition": condition, "verified": False, "error": str(error), "source": name}
            )
    return {
        "verified": not cancelled() and all(r["verified"] for r in receipts),
        "scope": "declared_conditions",
        "conditions": receipts,
        "observations": len(observations),
        "duration_ms": round((time.monotonic() - started) * 1000, 3),
    }
