from __future__ import annotations

import argparse
import json
import sys
import time
import uuid

from piper import PiperVoice, SynthesisConfig
from piper.config import PiperConfig
import onnxruntime


def _write_header(payload: dict[str, object]) -> None:
    sys.stdout.buffer.write(json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--config", required=True)
    arguments = parser.parse_args()

    # Piper's default ONNX pool uses extra threads. One non-spinning
    # thread is enough for this short speech job.
    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    options.add_session_config_entry("session.inter_op.allow_spinning", "0")
    with open(arguments.config, encoding="utf-8") as handle:
        config = PiperConfig.from_dict(json.load(handle))
    voice = PiperVoice(
        config=config,
        session=onnxruntime.InferenceSession(
            arguments.model,
            sess_options=options,
            providers=["CPUExecutionProvider"],
        ),
    )
    _write_header({"type": "ready", "sample_rate": voice.config.sample_rate})

    for raw_line in sys.stdin.buffer:
        request_id = uuid.uuid4().hex
        try:
            request = json.loads(raw_line)
            request_id = str(request.get("id", request_id))
            text = str(request["text"]).strip()
            if not text or len(text) > 2000:
                raise ValueError("speech text must be 1-2000 characters")
            speaking_rate = max(0.65, min(1.5, float(request.get("speaking_rate", 1.0))))
            synthesis = SynthesisConfig(
                length_scale=1.0 / speaking_rate,
                noise_scale=float(request.get("noise_scale", 0.62)),
                noise_w_scale=float(request.get("noise_w_scale", 0.72)),
                volume=max(0.1, min(2.0, float(request.get("volume", 0.9)))),
                normalize_audio=True,
            )
            silence = bytes(
                int(
                    voice.config.sample_rate
                    * max(0.0, float(request.get("sentence_silence", 0.16)))
                )
                * 2
            )
            started = time.monotonic()
            parts: list[bytes] = []
            for index, chunk in enumerate(voice.synthesize(text, syn_config=synthesis)):
                if index:
                    parts.append(silence)
                parts.append(chunk.audio_int16_bytes)
            pcm = b"".join(parts)
            if not pcm or len(pcm) % 2:
                raise RuntimeError("Piper produced invalid s16le audio")
            _write_header(
                {
                    "type": "audio",
                    "id": request_id,
                    "bytes": len(pcm),
                    "sample_rate": voice.config.sample_rate,
                    "channels": 1,
                    "sample_width": 2,
                    "latency_ms": round((time.monotonic() - started) * 1000, 3),
                }
            )
            sys.stdout.buffer.write(pcm)
            sys.stdout.buffer.flush()
        except Exception as error:
            _write_header({"type": "error", "id": request_id, "error": str(error)[:500]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
