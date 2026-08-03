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

1. Input shape is Silero's NATIVE streaming recipe, not ours: exactly
   512 samples (32 ms @ 16 kHz) per `prob()` call, with the last 64
   samples of the PREVIOUS chunk prepended internally so the graph
   always sees a 576-sample `input` tensor (zeros before the first
   chunk / after `reset()`). This mirrors upstream's own wrapper
   (snakers4/silero-vad v6.2.1, src/silero_vad/utils_vad.py,
   `OnnxWrapper.__call__`: 512-sample windows + 64-sample rolling
   context; `_validate_input` rejects windows under sr/31.25 = 512).
   An earlier revision fed raw 480-sample 30 ms hops on the grounds
   that the time axis is dynamic and "runs fine" — the graph does
   *execute* any length, but the output is garbage: measured on
   Silero's own 60 s English reference clip, raw 480- or 512-sample
   feeds (no context) score ~0.0005 on speech AND silence, never
   crossing 0.5 anywhere, while this recipe scores ~0.7-1.0 on speech
   vs ~0.01-0.07 on silence. One hop in, one probability out keeps
   `prob()` aligned 1:1 with the endpointer's 32 ms clock; the rolling
   context is internal and invisible to callers.
2. State handling: `stateN` from each call is fed back as `state` on
   the next — the classic integration gotcha. Zeros are the documented
   initial state. The state makes this object inherently stateful and
   per-stream: one `SileroVad` per uplink audio stream, never shared.
   `reset()` (not part of the `VadModel` Protocol — a deliberate small
   extra) restarts the recurrence at a call boundary without paying a
   session reload; it clears the recurrent state AND the rolling
   context together (mirroring upstream's `reset_states()` — clearing
   one but not the other would leak audio across the boundary).
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

# Silero v6's native streaming recipe at 16 kHz (judgment call 1):
# exactly 512 samples (32 ms) per prob() call, with the previous chunk's
# last 64 samples prepended to form the 576-sample input tensor.
FRAME_SAMPLES: Final[int] = 512
CONTEXT_SAMPLES: Final[int] = 64
_FRAME_BYTES: Final[int] = FRAME_SAMPLES * 2  # pcm16 = 2 bytes per sample

# The observed v6.2.1 signature (module docstring) — verified at load.
_EXPECTED_INPUTS: Final[frozenset[str]] = frozenset({"input", "state", "sr"})
_EXPECTED_OUTPUTS: Final[tuple[str, str]] = ("output", "stateN")
_STATE_SHAPE: Final[tuple[int, int, int]] = (2, 1, 128)

# pcm16 full scale for the int16 -> [-1.0, 1.0) float32 normalization.
_INT16_FULL_SCALE: Final[float] = 32768.0


class SileroVad:
    """Streaming Silero VAD: one speech probability per 512-sample
    (32 ms) pcm16 hop, recurrent state and 64-sample rolling context
    carried across calls (judgment calls 1-2)."""

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
        self._context: Any = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)
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
        """Speech probability in [0, 1] for one 512-sample pcm16 mono
        16 kHz hop. (The parameter name is pinned by the frozen
        `VadModel` Protocol in app/domain/voice.py and predates the
        32 ms correction — the hop is 32 ms; judgment call 1.)"""
        if len(frame_30ms) != _FRAME_BYTES:
            raise ValueError(
                f"pcm16 frame must be exactly {FRAME_SAMPLES} samples "
                f"({_FRAME_BYTES} bytes) — Silero's native 32 ms hop at 16 kHz "
                f"(judgment call 1) — got {len(frame_30ms)} bytes"
            )
        pcm = np.frombuffer(frame_30ms, dtype=np.int16)
        chunk = pcm.astype(np.float32) / _INT16_FULL_SCALE
        audio = np.concatenate([self._context, chunk]).reshape(1, -1)
        output, next_state = self._session.run(
            None, {"input": audio, "state": self._state, "sr": self._sr}
        )
        self._state = next_state
        self._context = chunk[-CONTEXT_SAMPLES:]
        return float(output[0][0])

    def reset(self) -> None:
        """Restart the recurrence (fresh stream/call boundary) — judgment
        call 2; cheaper than constructing a new session. Clears BOTH the
        recurrent state and the rolling context (clearing one but not
        the other would leak audio across the boundary)."""
        self._state = np.zeros(_STATE_SHAPE, dtype=np.float32)
        self._context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)


__all__ = [
    "CONTEXT_SAMPLES",
    "FRAME_SAMPLES",
    "SAMPLE_RATE_HZ",
    "SILERO_MODEL_PATH",
    "SileroVad",
]
