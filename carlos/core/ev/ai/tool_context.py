"""Bound local-model evidence without slicing JSON or partially copying targets.

This limits each result, not the whole tokenized context window. Full receipts
remain in the executor/journal; omitted observations must be requested narrowly.
"""

import json


def encode_tool_result(result, max_characters=6000):
    if not isinstance(result, dict):
        raise TypeError("Tool results must be objects")
    if not 2000 <= max_characters <= 32768:
        raise ValueError("Local tool-result limit must be 2000-32768 characters")
    encode = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    original = encode(result)
    if len(original) <= max_characters:
        return original
    summary = {
        "context_truncated": True,
        "original_characters": len(original),
        "context_note": "Only whole small fields are retained. Collections and large values were omitted, not observed absent. Do not infer missing targets or replay actions from this summary. Request a narrower fresh observation; full action receipts remain in task history.",
    }
    for key in ("status", "tool", "correlation_id", "error"):
        value = result.get(key)
        if isinstance(value, str) and len(value) <= 256:
            summary[key] = value
    execution = result.get("execution", result.get("verification", {}))
    if isinstance(execution, dict):
        # Retain the actual executor's scoped conclusion separately from the
        # incomplete observation. Do not manufacture a success/failure result.
        summary["reported_execution"] = {
            key: execution[key]
            for key in ("ok", "status", "verified", "scope", "changed_state")
            if key in execution
            and type(execution[key]) in (str, bool, type(None))
            and len(encode(execution[key])) < 100
        }
    data = result.get("result", {})
    retained, omitted = {}, []
    if isinstance(data, dict):
        for key, value in data.items():
            if (
                len(retained) < 12
                and isinstance(key, str)
                and len(key) <= 80
                and (
                    value is None
                    or type(value) in (bool, int, float)
                    or isinstance(value, str)
                    and len(value) <= 256
                )
                and len(encode(retained)) + len(encode({key: value})) < max_characters - 1300
            ):
                retained[key] = value
            elif len(omitted) < 20:
                omitted.append(str(key)[:80])
    summary["result_summary"] = retained
    summary["omitted_fields"] = omitted
    encoded = encode(summary)
    if len(encoded) > max_characters:
        summary.pop("omitted_fields")
        summary["result_summary"] = {}
        encoded = encode(summary)
    for optional in ("error", "correlation_id", "tool", "reported_execution"):
        if len(encoded) <= max_characters:
            break
        summary.pop(optional, None)
        encoded = encode(summary)
    return encoded
