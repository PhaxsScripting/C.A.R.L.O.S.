#!/usr/bin/env python3
"""Synthetic local vision check. No desktop capture, microphone, cloud or actions."""

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
from ev.ai.local_vision import LocalVisualReasoner
from ev.config import DEFAULT_CONFIG


async def run(scenario):
    from PIL import Image, ImageDraw, ImageFont

    with tempfile.TemporaryDirectory(prefix="ev-vision-fixture-") as directory:
        root = Path(directory)
        path = root / "fixture.png"
        image = Image.new("RGB", (512, 256), "white")
        draw = ImageDraw.Draw(image)
        if scenario == "dialog":
            image = Image.new("RGB", (640, 360), "#19202d")
            draw = ImageDraw.Draw(image)
            font = ImageFont.load_default(size=24)
            draw.rounded_rectangle((40, 40, 600, 320), radius=14, fill="#eeeeee")
            draw.text((68, 75), "Download failed", font=font, fill="black")
            draw.text((68, 125), "Network connection lost.", font=font, fill="black")
            draw.rounded_rectangle((345, 230, 465, 288), radius=6, fill="#2255dd")
            draw.text((368, 243), "Retry", font=font, fill="white")
            draw.text((480, 243), "Cancel", font=font, fill="black")
        else:
            left, right = ((35, 55, 185, 205), (325, 55, 475, 205))
            if scenario == "reversed":
                left, right = right, left
            draw.rectangle(left, fill="red")
            draw.ellipse(right, fill="blue")
        image.save(path)
        os.chmod(path, 0o600)
        model = LocalVisualReasoner(
            {**DEFAULT_CONFIG["vision"]["local_model"], "enabled": True}, root / "owner.json"
        )
        question = (
            "What error is displayed and which buttons are visible? Describe only, do not act."
            if scenario == "dialog"
            else "Name the two colored shapes and say which is on the left and which is on the right."
        )
        result = await model.describe(path, question)
        text = result["description"].casefold()
        required = (
            ("download", "network", "retry", "cancel")
            if scenario == "dialog"
            else ("red", "square", "blue", "circle", "left", "right")
        )
        passed = all(w in text for w in required)
        print(
            json.dumps(
                {
                    **result,
                    "synthetic_check_passed": passed,
                    "scenario": scenario,
                    "check_scope": "expected word coverage; relationships require human review",
                    "desktop_capture": False,
                }
            ),
            flush=True,
        )
        if (root / "owner.json").exists():
            raise RuntimeError("Vision runtime ownership cleanup did not finish")
        return passed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--scenario", choices=["shapes", "reversed", "dialog"], default="shapes")
    args = parser.parse_args()
    if not args.run:
        parser.error("Pass --run to load the local multimodal model")
    sys.exit(0 if asyncio.run(run(args.scenario)) else 1)
