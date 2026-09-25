"""Evidence-based capability status without treating registration as success."""

from collections import defaultdict


def capability_evidence(catalog, events, input_status):
    samples = defaultdict(list)
    for event in events:
        if event.get("type") in {"tool.completed", "tool.failed"}:
            samples[event.get("payload", {}).get("tool")].append(event)
    rows = []
    for tool in catalog:
        name = tool["name"]
        history = samples[name]
        backend = {
            key: None
            for key in ("installed", "available", "connected", "authorized", "functioning")
        }
        native_input = name.startswith(("desktop.pointer.", "desktop.keyboard.", "desktop.input."))
        if native_input:
            backend.update(
                {
                    key: input_status.get(key)
                    for key in ("installed", "available", "connected", "authorized")
                }
            )
            backend["name"] = input_status.get("backend", "xdg-desktop-portal")
            backend["evidence"] = input_status.get(
                "availability_evidence", "Dependency/grant status, not target-specific execution"
            )
        last = history[-1] if history else {}
        payload = last.get("payload", {})
        execution = payload.get("execution", {})
        last_result = payload.get("result", {})
        if isinstance(last_result, dict) and "available" in last_result and not native_input:
            backend["last_reported_available"] = last_result["available"]
            backend["name"] = last_result.get("backend")
            # Old probes are history, not current readiness.
        success = sum(
            e["type"] == "tool.completed" and e.get("payload", {}).get("ok") is True
            for e in history
        )
        verified = sum(
            e.get("payload", {}).get("execution", {}).get("verified") is True for e in history
        )
        rows.append(
            {
                **tool,
                "implementation": (
                    "RETIRED" if name == "development.build_project" else "REGISTERED"
                ),
                "backend_state": backend,
                "execution_evidence": {
                    "observations": len(history),
                    "accepted_or_completed": success,
                    "verified_results": verified,
                    "failures": sum(
                        e["type"] == "tool.failed" or e.get("payload", {}).get("ok") is False
                        for e in history
                    ),
                    "last_observed_at": last.get("timestamp"),
                    "last_execution_status": execution.get("status"),
                    "last_verification_scope": execution.get("scope"),
                    "verification_fraction": verified / len(history) if history else None,
                    "independent_goal_success_rate": None,
                    "scope": "Bounded runtime receipts; not a general reliability score or current target guarantee",
                },
            }
        )
    return rows
