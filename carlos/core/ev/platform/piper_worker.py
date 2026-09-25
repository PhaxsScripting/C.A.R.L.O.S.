"""FreeBSD native Piper CLI behind E.V.'s existing framed PCM protocol."""

import argparse
import json
import subprocess
import sys
import time
from array import array


def header(value):
    sys.stdout.buffer.write(json.dumps(value).encode() + b"\n")
    sys.stdout.buffer.flush()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    with open(args.config) as f:
        rate = int(json.load(f)["audio"]["sample_rate"])
    header({"type": "ready", "sample_rate": rate})
    for raw in sys.stdin.buffer:
        request_id = None
        try:
            request = json.loads(raw)
            request_id = request["id"]
            text = str(request["text"]).strip()
            if not 1 <= len(text) <= 2000:
                raise ValueError("Speech must be 1-2000 characters")
            started = time.monotonic()
            speed = max(0.65, min(1.5, float(request.get("speaking_rate", 1))))
            result = subprocess.run(
                [
                    "/usr/local/bin/piper",
                    "--model",
                    args.model,
                    "--config",
                    args.config,
                    "--output-raw",
                    "--length_scale",
                    str(1 / speed),
                    "--noise_scale",
                    str(float(request.get("noise_scale", 0.62))),
                    "--noise_w",
                    str(float(request.get("noise_w_scale", 0.72))),
                    "--sentence_silence",
                    str(max(0.0, float(request.get("sentence_silence", 0.16)))),
                ],
                input=(text + "\n").encode(),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=25,
                check=True,
            )
            pcm = result.stdout
            if not pcm or len(pcm) > 20_000_000 or len(pcm) % 2:
                raise RuntimeError("Invalid native Piper PCM")
            volume = max(0.1, min(2.0, float(request.get("volume", 0.9))))
            samples = array("h")
            samples.frombytes(pcm)
            if sys.byteorder != "little":
                samples.byteswap()
            samples = array("h", (max(-32768, min(32767, round(s * volume))) for s in samples))
            if sys.byteorder != "little":
                samples.byteswap()
            pcm = samples.tobytes()
            header(
                {
                    "type": "audio",
                    "id": request_id,
                    "bytes": len(pcm),
                    "sample_rate": rate,
                    "channels": 1,
                    "sample_width": 2,
                    "latency_ms": (time.monotonic() - started) * 1000,
                }
            )
            sys.stdout.buffer.write(pcm)
            sys.stdout.buffer.flush()
        except Exception as error:
            # Never echo speech text or subprocess command into diagnostics.
            header({"type": "error", "id": request_id, "error": type(error).__name__})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
