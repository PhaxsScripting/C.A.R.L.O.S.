from __future__ import annotations

import json
from typing import Any

PROTOCOL_VERSION = 1


class ProtocolError(ValueError):
    pass


def decode_message(line: bytes, maximum: int) -> dict[str, Any]:
    if len(line) > maximum:
        raise ProtocolError("message_too_large")
    try:
        value = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProtocolError("invalid_json") from error
    if not isinstance(value, dict):
        raise ProtocolError("message_must_be_object")
    version = value.get("protocol_version", PROTOCOL_VERSION)
    if type(version) is not int or version != PROTOCOL_VERSION:
        raise ProtocolError("unsupported_protocol_version")
    message_type = value.get("type")
    if not isinstance(message_type, str) or not message_type or len(message_type) > 96:
        raise ProtocolError("invalid_message_type")
    request_id = value.get("id")
    if request_id is not None and (not isinstance(request_id, str) or len(request_id) > 128):
        raise ProtocolError("invalid_request_id")
    payload = value.get("payload", {})
    if not isinstance(payload, dict):
        raise ProtocolError("payload_must_be_object")
    return {"type": message_type, "id": request_id, "payload": payload}


def encode_message(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
