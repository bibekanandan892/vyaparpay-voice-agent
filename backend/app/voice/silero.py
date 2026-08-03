"""SileroVad — the real `VadModel` (docs/06 §5) over onnxruntime and the
pinned Silero VAD v6.2.1 ONNX model `scripts/fetch_models.py` downloads.

THE ONLY MODULE ALLOWED TO IMPORT onnxruntime — the same confinement rule
docs/06 §1.1 applies to aiortc (PeerSession only). Everything else speaks
`VadModel.prob()`; tests script `FakeVad` instead.

Observed model signature (introspected from the loaded v6.2.1 session,
NOT copied from blog posts about older releases — v3/v4 had separate
`h`/`c` LSTM tensors, this model does not):

    inputs:  input  float32 [batch, samples]   (both axes dynamic)
             state  float32 [2, batch, 128]    (merged recurrent state)
             sr     int64   []                 (scalar sample rate)
    outputs: output float32 [batch, 1]         (speech probability)
             stateN float32 [2, batch, 128]    (next-call state)

`_verify_signature()` re-checks this at load time and fails loudly if a
re-pinned model ever drifts, instead of feeding tensors into the wrong
graph.

Judgment calls, numbered:

1. The 480-sample 30 ms hop (the `AudioFrame` contract) is fed to the
   graph AS-IS. The `input` time axis is dynamic and a 480-sample frame
   runs fine (verified empirically at pin time; upstream's own Python
   wrapper happens to slice 512-sample windows, but that is wrapper
   policy, not a graph constraint). No internal re-buffering to 512 —
   one hop in, one probability out keeps `prob()` aligned 1:1 with the
   endpointer's 30 ms clock.
2. State handling: `stateN` from each call is fed back as `state` on
   the next — the classic integration gotcha. Zeros are the documented
   initial state. The state makes this object inherently stateful and
   per-stream: one `SileroVad` per uplink audio stream, never shared.
   `reset()` (not part of the `VadModel` Protocol — a deliberate small
   extra) restarts the recurrence at a call boundary without paying a
   session reload.
3. `sr` is pinned to 16 kHz as a module constant, not a parameter —
   docs/06 §3.2 fixes the worker-internal STT/VAD format; a
   configurable rate here would just be an untested code path.
4. The ORT session runs single-threaded (intra/inter op = 1),
   mirroring upstream's wrapper: the model is ~2.3 MB and a frame
   inference is sub-millisecond; thread-pool fan-out would only add
   contention inside the worker's asyncio process.
5. `CPUExecutionProvider` is pinned explicitly so a dev box with a GPU
   build of onnxruntime can't silently pick a different provider with
   different numerics — determinism across identical input+state
   sequences is a tested property.

Run `python -m scripts.fetch_models` from backend/ to populate the
model file; the constructor's FileNotFoundError says the same.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import numpy as np
import onnxruntime  # see module docstring: the ONLY module allowed to import this

# Where scripts/fetch_models.py lands the pinned model, resolved relative
# to this file so imports work regardless of CWD.
SILERO_MODEL_PATH: Final[Path] = Path(__file__).resolve().parent / "models" / "silero_vad.onnx"

# docs/06 §3.2: the worker-internal VAD/STT format is 16 kHz mono pcm16.
SAMPLE_RATE_HZ: Final[int] = 16_000

# The observed v6.2.1 signature (module docstring) — verified at load.
_EXPECTED_INPUTS: Final[frozenset[str]] = frozenset({"input", "state", "sr"})
_EXPECTED_OUTPUTS: Final[tuple[str, str]] = ("output", "stateN")
_STATE_SHAPE: Final[tuple[int, int, int]] = (2, 1, 128)

# pcm16 full scale for the int16 -> [-1.0, 1.0) float32 normalization.
_INT16_FULL_SCALE: Final[float] = 32768.0


class SileroVad:
    """Streaming Silero VAD: one speech probability per 30 ms pcm16 hop,
    recurrent state carried across calls (judgment call 2)."""

    def __init__(self, model_path: Path = SILERO_MODEL_PATH) -> None:
        if not model_path.exists():
            raise FileNotFoundError(
                f"Silero VAD model not found at {model_path} — run "
                "`python -m scripts.fetch_models` from backend/ to download the "
                "pinned model (CI's gates job does this before running tests)."
            )
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        self._session = onnxruntime.InferenceSession(
            str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self._verify_signature()
        self._state: Any = np.zeros(_STATE_SHAPE, dtype=np.float32)
        self._sr = np.array(SAMPLE_RATE_HZ, dtype=np.int64)

    def _verify_signature(self) -> None:
        """Fail loudly if a re-pinned model stops matching the introspected
        v6.2.1 signature this class was written against (module docstring)."""
        inputs = frozenset(i.name for i in self._session.get_inputs())
        outputs = tuple(o.name for o in self._session.get_outputs())
        if inputs != _EXPECTED_INPUTS or outputs != _EXPECTED_OUTPUTS:
            raise RuntimeError(
                f"unexpected Silero ONNX signature: inputs={sorted(inputs)}, "
                f"outputs={list(outputs)} — expected inputs={sorted(_EXPECTED_INPUTS)}, "
                f"outputs={list(_EXPECTED_OUTPUTS)}. The pinned model has changed shape; "
                "re-introspect it and update app/voice/silero.py deliberately."
            )

    def prob(self, frame_30ms: bytes) -> float:
        """Speech probability in [0, 1] for one pcm16 mono 16 kHz hop."""
        if not frame_30ms or len(frame_30ms) % 2:
            raise ValueError(
                f"pcm16 frame must be a non-empty even number of bytes, got {len(frame_30ms)}"
            )
        pcm = np.frombuffer(frame_30ms, dtype=np.int16)
        audio = (pcm.astype(np.float32) / _INT16_FULL_SCALE).reshape(1, -1)
        output, next_state = self._session.run(
            None, {"input": audio, "state": self._state, "sr": self._sr}
        )
        self._state = next_state
        return float(output[0][0])

    def reset(self) -> None:
        """Restart the recurrence (fresh stream/call boundary) — judgment
        call 2; cheaper than constructing a new session."""
        self._state = np.zeros(_STATE_SHAPE, dtype=np.float32)


__all__ = ["SAMPLE_RATE_HZ", "SILERO_MODEL_PATH", "SileroVad"]
