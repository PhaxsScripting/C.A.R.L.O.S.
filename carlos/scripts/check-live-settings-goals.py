#!/usr/bin/env python3
"""Read-only installed-core settings predicate check; never changes settings.

Expected values deliberately come from current observations for this backend
fixture. This is not model interpretation or proof of a user-requested change.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from ev.cli import request


async def tool(name, arguments):
    return (await request("tool.call", {"name": name, "arguments": arguments}))["payload"]


async def run():
    snapshot = (await request("snapshot"))["payload"]
    if snapshot["core"]["state"] != "DORMANT" or snapshot["planner"]["active"]:
        raise RuntimeError("Core is busy; refusing to interfere with a task")
    results = []
    cases = [
        ("power_profile", "settings.power_profile.get", {}),
        ("screen_brightness", "settings.brightness.get", {}),
        (
            "default_application",
            "settings.default_application.get",
            {"mime_type": "application/pdf"},
        ),
        ("night_light_state", "settings.night_light.inspect", {}),
    ]
    for kind, name, arguments in cases:
        observation = await tool(name, arguments)
        data = observation.get("result", {})
        if observation.get("status") != "completed" or data.get("available") is False:
            results.append(
                {
                    "kind": kind,
                    "status": "UNAVAILABLE",
                    "reason": "Native observation unavailable; no condition certified",
                }
            )
            continue
        condition = {"kind": kind}
        if kind == "power_profile":
            condition["expected"] = data["current"]
            wrong = "ev-fixture-nonexistent-profile"
            assert wrong not in data["profiles"]
        elif kind == "screen_brightness":
            condition["expected"] = data["percent"]
            wrong = 0 if data["percent"] > 50 else 100
        elif kind == "default_application":
            if not data.get("desktop_id"):
                results.append(
                    {
                        "kind": kind,
                        "status": "NO_HANDLER",
                        "reason": "No PDF handler configured; nothing changed",
                    }
                )
                continue
            condition.update(mime_type=arguments["mime_type"], expected=data["desktop_id"])
            wrong = "ev-fixture-nonexistent-handler.desktop"
            assert wrong != data["desktop_id"]
        else:
            condition.update(property="enabled", expected=data["enabled"])
            wrong = not data["enabled"]
        correct = await tool("agent.verify_conditions", {"conditions": [condition]})
        assert correct["status"] == "completed" and correct["result"]["verified"], correct
        mismatched = await tool(
            "agent.verify_conditions", {"conditions": [{**condition, "expected": wrong}]}
        )
        assert mismatched["status"] == "failed" and not mismatched["result"]["verified"], mismatched
        results.append({"kind": kind, "status": "PASSED", "wrong_expected_rejected": True})
    assert any(row["status"] == "PASSED" for row in results), results
    print(
        json.dumps(
            {
                "installed_settings_predicates": results,
                "settings_changed": False,
                "model_requests": 0,
                "goal_interpretation_tested": False,
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    if not parser.parse_args().run:
        parser.error("Pass --run for read-only native settings verification")
    asyncio.run(run())
