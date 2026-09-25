#!/usr/bin/env python3
"""Opt-in installed-core smoke check, restricted to self-created file fixtures.

No cloud/model request, speech, user document edit or approval-token handling.
"""

import argparse
import hashlib
import json
import subprocess
import tempfile
import zipfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    if not args.run:
        parser.error("Pass --run to create disposable fixtures and test the running core")
    evctl = Path.home() / ".local/bin/evctl"

    def call(name, arguments):
        completed = subprocess.run(
            [str(evctl), "tool", name, "--arguments", json.dumps(arguments)],
            capture_output=True,
            text=True,
            timeout=35,
        )
        response = json.loads(completed.stdout)
        if response.get("type") != "response":
            raise RuntimeError(f"{name}: {response}")
        return response["payload"]

    with tempfile.TemporaryDirectory(
        prefix=".ev-file-check-", dir=Path.home() / "Downloads"
    ) as temporary:
        root = Path(temporary)
        source, destination = root / "fixture.zip", root / "extracted"
        content = b"E.V. installed-core file check\n"
        with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("nested/fixture.txt", content)
        inspection = call("files.archive_inspect", {"path": str(source)})
        assert inspection["status"] == "completed", inspection
        assert inspection["result"]["entries_count"] == 1, inspection
        assert inspection["result"]["payload_verified"] is False, inspection
        extracted = call(
            "files.archive_extract", {"path": str(source), "destination": str(destination)}
        )
        assert extracted["status"] == "completed", extracted
        assert extracted["execution"]["verified"] is True, extracted
        assert (destination / "nested/fixture.txt").read_bytes() == content
        hashed = call("files.hash", {"path": str(destination / "nested/fixture.txt")})
        assert hashed["result"]["sha256"] == hashlib.sha256(content).hexdigest(), hashed
        planned_file = root / "planned.txt"
        plan = call(
            "agent.execute_plan",
            {
                "goal": "Create and verify a disposable fixture",
                "steps": [
                    {
                        "group": "fixture",
                        "steps": [
                            {
                                "id": "1",
                                "tool": "files.text.create",
                                "arguments": {
                                    "path": str(planned_file),
                                    "content": "planned fixture",
                                },
                            }
                        ],
                    }
                ],
                "conditions": [
                    {
                        "kind": "file_hash",
                        "path": {"$ref": "1.result.path"},
                        "sha256": hashlib.sha256(b"planned fixture").hexdigest(),
                    },
                    {
                        "kind": "file_text",
                        "path": {"$ref": "1.result.path"},
                        "expected": "planned fixture",
                    },
                ],
            },
        )
        assert plan["status"] == "completed", plan
        assert plan["result"]["goal_verification"]["verified"] is True, plan
        assert planned_file.read_text() == "planned fixture"
        mismatch = call(
            "agent.verify_conditions",
            {
                "conditions": [
                    {
                        "kind": "file_text",
                        "path": str(planned_file),
                        "expected": "planned fixture\n",
                    }
                ]
            },
        )
        assert mismatch["status"] == "failed", mismatch
        assert mismatch["result"]["verified"] is False, mismatch
        source_context = call(
            "development.source.inspect", {"path": str(planned_file), "line": 1, "context_lines": 0}
        )
        assert source_context["status"] == "completed", source_context
        assert source_context["result"]["lines"] == [
            {"line": 1, "text": "planned fixture"}
        ], source_context
        assert (
            source_context["result"]["sha256"]
            == hashlib.sha256(planned_file.read_bytes()).hexdigest()
        )
        print(
            json.dumps(
                {
                    "installed_archive_inspection": "passed",
                    "installed_extraction": "passed",
                    "installed_composite_plan_and_postcondition": "passed",
                    "installed_numbered_id_and_exact_text": "passed",
                    "different_newline_rejected": "passed",
                    "installed_source_location_inspection": "passed",
                    "independent_file_readback": "passed",
                    "installed_hash_readback": "passed",
                    "fixture_cleanup": "automatic",
                    "cloud_requests": 0,
                    "user_documents_changed": 0,
                }
            )
        )


if __name__ == "__main__":
    main()
