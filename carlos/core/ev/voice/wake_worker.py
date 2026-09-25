from __future__ import annotations

import argparse
import json
import os
import sys
import time
from array import array


def emit(kind: str, **payload: object) -> None:
    print(json.dumps({"type": kind, **payload}, separators=(",", ":")), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Isolated local E.V. wake-word worker")
    parser.add_argument("--tokens", required=True)
    parser.add_argument("--encoder", required=True)
    parser.add_argument("--decoder", required=True)
    parser.add_argument("--joiner", required=True)
    parser.add_argument("--keywords", required=True)
    parser.add_argument("--score", type=float, default=1.5)
    parser.add_argument("--threshold", type=float, default=0.18)
    parser.add_argument("--threads", type=int, default=1)
    arguments = parser.parse_args()

    try:
        import sherpa_onnx

        spotter = sherpa_onnx.KeywordSpotter(
            tokens=arguments.tokens,
            encoder=arguments.encoder,
            decoder=arguments.decoder,
            joiner=arguments.joiner,
            keywords_file=arguments.keywords,
            num_threads=max(1, min(2, arguments.threads)),
            keywords_score=max(0.1, min(5.0, arguments.score)),
            keywords_threshold=max(0.01, min(0.95, arguments.threshold)),
            provider="cpu",
        )
        stream = spotter.create_stream()
        emit("ready", pid=os.getpid(), engine="sherpa-onnx", sample_rate=16000)
        input_stream = sys.stdin.buffer
        processed_bytes = 0
        last_progress_bytes = 0
        while True:
            pcm = input_stream.read(1600)
            if not pcm:
                break
            if len(pcm) % 2:
                pcm = pcm[:-1]
            samples_i16 = array("h")
            samples_i16.frombytes(pcm)
            if sys.byteorder != "little":
                samples_i16.byteswap()
            samples_f32 = array("f", (sample / 32768.0 for sample in samples_i16))
            stream.accept_waveform(16000, samples_f32)
            while spotter.is_ready(stream):
                spotter.decode_stream(stream)
                result = spotter.get_result(stream)
                if result:
                    tokens = spotter.tokens(stream)
                    timestamps = spotter.timestamps(stream)
                    emit(
                        "detected",
                        keyword=result,
                        tokens=tokens,
                        timestamps=timestamps,
                        worker_monotonic=time.monotonic(),
                    )
                    spotter.reset_stream(stream)
            processed_bytes += len(pcm)
            if processed_bytes - last_progress_bytes >= 32000:
                emit("progress", processed_bytes=processed_bytes)
                last_progress_bytes = processed_bytes
        return 0
    except Exception as error:
        emit("error", error=f"{type(error).__name__}: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
