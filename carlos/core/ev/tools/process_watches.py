from ..permissions import Permission
from .base import ToolSpec
from .builtin import object_schema


def register_process_watch_tools(registry, watches):
    identity = {
        "pid": {"type": "integer", "minimum": 1, "maximum": 4194304},
        "start_ticks": {"type": "string", "pattern": "^[0-9]{1,24}$"},
        "boot_id": {"type": "string", "pattern": "^[0-9a-f-]{36}$"},
    }

    def create(a, c):
        record = watches.create({k: a[k] for k in identity}, a.get("timeout_seconds", 3600))
        return {
            "verified": True,
            "watch": record,
            "verification_scope": "watch_registration_only",
            "processes_signalled": 0,
        }

    registry.register(
        ToolSpec(
            "agent.watch_process",
            "AGENT",
            "Register a durable background watch for an exact currently running same-user PID/start_ticks/boot_id from system.process_lifetime. Observe after E.V. restarts, for at most 24 hours. Does not wait in the foreground, run follow-up actions, signal the process or claim its job succeeded. Inspect agent.process_watches for outcomes.",
            Permission.LOW_RISK,
            object_schema(
                {**identity, "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 86400}},
                list(identity),
            ),
            create,
        )
    )
    registry.register(
        ToolSpec(
            "agent.process_watches",
            "AGENT",
            "Inspect durable background process-lifetime watch statuses. Lifetime ending is not job success or known exit code.",
            Permission.SAFE,
            object_schema({}),
            lambda a, c: {"watches": watches.list(), "actions_replayed": False},
            read_only=True,
        )
    )
    registry.register(
        ToolSpec(
            "agent.cancel_process_watch",
            "AGENT",
            "Cancel one exact background watch without stopping or signalling the watched application.",
            Permission.LOW_RISK,
            object_schema({"id": {"type": "string", "pattern": "^[0-9a-f]{32}$"}}, ["id"]),
            lambda a, c: watches.cancel(a["id"]),
        )
    )
