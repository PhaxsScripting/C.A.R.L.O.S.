from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
    "audio",
    "content",
    "file_content",
    "screen_image",
    "text",
    "old_text",
    "new_text",
}


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower() in SENSITIVE_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str) and len(value) > 4096:
        return value[:4096] + "...[TRUNCATED]"
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload["fields"] = redact(fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(log_file: Path, verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger("ev")
    logger.disabled = False
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False

    file_handler = RotatingFileHandler(
        log_file, maxBytes=2_000_000, backupCount=4, encoding="utf-8"
    )
    file_handler.setFormatter(JsonFormatter())
    logger.addHandler(file_handler)

    stderr_handler = logging.StreamHandler()
    stderr_handler.setLevel(logging.DEBUG if verbose else logging.WARNING)
    stderr_handler.setFormatter(logging.Formatter("Carlos %(levelname)s: %(message)s"))
    logger.addHandler(stderr_handler)
    return logger
