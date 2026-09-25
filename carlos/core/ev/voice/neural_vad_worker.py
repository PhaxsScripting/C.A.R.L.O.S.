"""Silero v6 ONNX inference. Mono 16 kHz PCM enters only a local pipe.

Model contract: 512 new samples plus 64 context samples, recurrent state
(2, 1, 128). No torch import, network access, or audio retention.
"""

from __future__ import annotations

import base64
import json
import sys


def main() -> None:
    import numpy as np
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(
        sys.argv[1], sess_options=options, providers=["CPUExecutionProvider"]
    )
    previous = None
    pending = bytearray()
    state = np.zeros((2, 1, 128), dtype=np.float32)
    context = np.zeros((1, 64), dtype=np.float32)
    last_probability = 0.0
    print(json.dumps({"ready": True}), flush=True)
    while line := sys.stdin.buffer.readline(16384):
        request = json.loads(line)
        if request["capture"] != previous:
            previous = request["capture"]
            pending.clear()
            state.fill(0)
            context.fill(0)
            last_probability = 0.0
        pending.extend(base64.b64decode(request["pcm"], validate=True))
        probabilities = []
        while len(pending) >= 1024:
            samples = (
                np.frombuffer(bytes(pending[:1024]), dtype="<i2").astype(np.float32).reshape(1, -1)
                / 32768.0
            )
            del pending[:1024]
            samples = np.concatenate((context, samples), axis=1)
            probability, state = session.run(
                None, {"input": samples, "state": state, "sr": np.array(16000, dtype=np.int64)}
            )
            context = samples[:, -64:]
            probabilities.append(float(probability[0][0]))
        if probabilities:
            last_probability = max(probabilities)
        print(json.dumps({"probability": last_probability}), flush=True)


if __name__ == "__main__":
    main()
