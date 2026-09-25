from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Isolated local E.V. OCR worker")
    parser.add_argument("--image", required=True)
    parser.add_argument("--minimum-score", type=float, default=0.55)
    arguments = parser.parse_args()
    path = Path(arguments.image)
    if not path.is_file() or path.suffix.casefold() != ".png":
        raise SystemExit("input is not a PNG file")
    minimum = max(0.0, min(1.0, arguments.minimum_score))
    started = time.perf_counter()
    from rapidocr import RapidOCR

    output = RapidOCR()(path)
    elements = []
    boxes = output.boxes if output.boxes is not None else []
    texts = output.txts if output.txts is not None else []
    scores = output.scores if output.scores is not None else []
    for box, text, score in zip(boxes, texts, scores, strict=False):
        confidence = float(score)
        if confidence < minimum:
            continue
        points = [[round(float(point[0]), 1), round(float(point[1]), 1)] for point in box]
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        elements.append(
            {
                "text": str(text),
                "confidence": round(confidence, 4),
                "box": points,
                "center": {"x": round(sum(xs) / len(xs), 1), "y": round(sum(ys) / len(ys), 1)},
            }
        )
        if len(elements) >= 250:
            break
    payload = {
        "engine": "RapidOCR 3.9.2 / ONNX Runtime",
        "elements": elements,
        "text": "\n".join(item["text"] for item in elements),
        "count": len(elements),
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
    }
    print(
        "EV_OCR_JSON:" + json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
