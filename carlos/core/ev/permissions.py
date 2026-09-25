from __future__ import annotations

import hashlib
import json
import secrets
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class Permission(StrEnum):
    SAFE = "SAFE"
    LOW_RISK = "LOW_RISK"
    SENSITIVE = "SENSITIVE"
    HIGH = "HIGH"
    PRIVILEGED = "PRIVILEGED"
    DESTRUCTIVE = "DESTRUCTIVE"


@dataclass(slots=True)
class PendingPermission:
    id: str
    token: str
    tool_name: str
    arguments: dict[str, Any]
    arguments_hash: str
    permission: Permission
    reason: str
    correlation_id: str
    created_monotonic: float
    expires_monotonic: float

    def public(self, include_token: bool = True) -> dict[str, Any]:
        result = {
            "id": self.id,
            "tool": self.tool_name,
            "arguments": self.arguments,
            "permission": self.permission.value,
            "reason": self.reason,
            "correlation_id": self.correlation_id,
            "expires_in_seconds": max(0, round(self.expires_monotonic - time.monotonic())),
        }
        if include_token:
            result["approval_token"] = self.token
        return result


class PermissionBroker:
    def __init__(self, timeout_seconds: int = 90) -> None:
        self.timeout_seconds = max(15, min(timeout_seconds, 600))
        self._pending: dict[str, PendingPermission] = {}

    @staticmethod
    def arguments_hash(tool_name: str, arguments: dict[str, Any]) -> str:
        canonical = json.dumps(
            {"tool": tool_name, "arguments": arguments}, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def create(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        permission: Permission,
        reason: str,
        correlation_id: str,
    ) -> PendingPermission:
        pending_id = uuid.uuid4().hex
        now = time.monotonic()
        pending = PendingPermission(
            id=pending_id,
            token=secrets.token_urlsafe(32),
            tool_name=tool_name,
            arguments=arguments,
            arguments_hash=self.arguments_hash(tool_name, arguments),
            permission=permission,
            reason=reason,
            correlation_id=correlation_id,
            created_monotonic=now,
            expires_monotonic=now + self.timeout_seconds,
        )
        self._pending[pending_id] = pending
        return pending

    def resolve(
        self, pending_id: str, token: str, approved: bool
    ) -> tuple[PendingPermission, bool]:
        pending = self._pending.get(pending_id)
        if pending is None or pending.expires_monotonic <= time.monotonic():
            raise ValueError("confirmation not found or expired")
        if not secrets.compare_digest(pending.token, token):
            raise ValueError("invalid confirmation token")
        self._pending.pop(pending_id, None)
        return pending, approved

    def cancel(self, pending_id: str) -> PendingPermission | None:
        """Revoke a pending decision without executing its action."""

        return self._pending.pop(pending_id, None)

    def prune(self) -> list[PendingPermission]:
        now = time.monotonic()
        expired: list[PendingPermission] = []
        for pending_id, pending in tuple(self._pending.items()):
            if pending.expires_monotonic <= now:
                self._pending.pop(pending_id, None)
                expired.append(pending)
        return expired

    def list_public(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        return [
            pending.public(include_token=False)
            for pending in self._pending.values()
            if pending.expires_monotonic > now
        ]

    def list_for_local_client(self) -> list[dict[str, Any]]:
        """Return actionable confirmations to an already authenticated local IPC peer."""

        now = time.monotonic()
        return [
            pending.public(include_token=True)
            for pending in self._pending.values()
            if pending.expires_monotonic > now
        ]
