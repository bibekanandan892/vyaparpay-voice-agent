"""Real-model smoke tests for app.voice.silero.SileroVad — the ONE test
module that loads the actual pinned ONNX model through onnxruntime.

Deliberately requires NO speech audio: everything here is provable with
digital silence and seeded pseudo-random noise — load, output range,
low-probability-on-silence, determinism, and 100-frame state stability.
Whether the model detects *speech* well is Silero's benchmark, not ours;
what we own is the integration (tensor names, state feedback, pcm16
normalization), and that is what breaks in practice.

SKIP GUARD — read before assuming these never run: the module skips only
when the [voice] extra (onnxruntime) or the fetched model file is absent,
so a Phase-2-only checkout still passes `pytest tests`. CI's gates job
DOES install `.[dev,voice]` AND runs `python -m scripts.fetch_models`
before pytest, so these tests execute on every CI run — the skip is a
local-dev convenience, never a way for CI to quietly drop coverage.
"""

from __future__ import annotations

import pytest

np = pytest.importorskip("numpy", reason="[voice] extra not installed")
pytest.importorskip("onnxruntime", reason="[voice] extra not installed")

from app.voice.silero import SILERO_MODEL_PATH, SileroVad  # noqa: E402

pytestmark = pytest.mark.skipif(
    not SILERO_MODEL_PATH.exists(),
    reason=(
        "Silero model not fetched — run `python -m scripts.fetch_models` from "
        "backend/ (CI's gates job always fetches it, so these run there)"
    ),
)

FRAME_SAMPLES = 480  # one 30 ms hop @ 16 kHz (docs/06 §5)
FRAME_BYTES = FRAME_SAMPLES * 2  # pcm16
SILENCE_FRAME = bytes(FRAME_BYTES)  # pure digital zeros
ACTIVATION_THRESHOLD = 0.5  # docs/06 §5's default vad_threshold
STABILITY_RUN_FRAMES = 100


def _noise_frames(n: int, seed: int = 1234) -> list[bytes]:
    """Seeded low-level pcm16 noise — deterministic non-constant input
    that exercises the recurrent state without needing speech audio."""
    rng = np.random.default_rng(seed)
    return [
        rng.integers(-2000, 2000, FRAME_SAMPLES, dtype=np.int16).tobytes() for _ in range(n)
    ]


def test_model_loads_and_signature_verification_passes() -> None:
    """Constructing SileroVad loads the pinned model AND `_verify_signature`
    accepts the observed v6 signature (input/state/sr -> output/stateN)."""
    SileroVad()


def test_prob_returns_a_probability_in_unit_interval() -> None:
    vad = SileroVad()
    p = vad.prob(SILENCE_FRAME)
    assert 0.0 <= p <= 1.0


def test_digital_silence_scores_below_activation_threshold_across_a_run() -> None:
    """Pure zeros must never look like speech: every frame of a 20-frame
    silent run stays under the 0.5 activation threshold — otherwise the
    endpointer's trailing-silence clock could never start."""
    vad = SileroVad()
    probs = [vad.prob(SILENCE_FRAME) for _ in range(20)]
    assert all(p < ACTIVATION_THRESHOLD for p in probs), f"silence scored {max(probs)}"


def test_prob_is_deterministic_for_identical_input_and_state_sequence() -> None:
    """Two fresh instances fed the same frame sequence produce bit-equal
    probabilities — the CPUExecutionProvider pin plus zero initial state
    make the whole recurrence reproducible."""
    frames = _noise_frames(10)
    first = SileroVad()
    second = SileroVad()
    assert [first.prob(f) for f in frames] == [second.prob(f) for f in frames]


def test_state_feedback_survives_100_consecutive_frames() -> None:
    """The stateN -> state feedback loop (the classic integration gotcha)
    holds up over a long alternating run: no shape drift, no crash, and
    every output stays a valid probability."""
    vad = SileroVad()
    noise = _noise_frames(STABILITY_RUN_FRAMES // 2, seed=7)
    for i in range(STABILITY_RUN_FRAMES):
        frame = SILENCE_FRAME if i % 2 == 0 else noise[i // 2]
        p = vad.prob(frame)
        assert 0.0 <= p <= 1.0


def test_reset_restores_the_initial_state() -> None:
    """After reset() the recurrence starts over: a frame scored on a
    fresh instance and the same frame scored after reset() on a used
    instance agree exactly (determinism makes this an == check)."""
    probe = _noise_frames(1, seed=99)[0]
    baseline = SileroVad().prob(probe)

    used = SileroVad()
    for frame in _noise_frames(10):
        used.prob(frame)
    used.reset()
    assert used.prob(probe) == baseline


def test_missing_model_file_raises_actionable_error(tmp_path) -> None:
    with pytest.raises(FileNotFoundError, match="fetch_models"):
        SileroVad(model_path=tmp_path / "nowhere" / "silero_vad.onnx")


@pytest.mark.parametrize("bad_frame", [b"", b"\x00"])
def test_rejects_empty_or_odd_length_frames(bad_frame: bytes) -> None:
    vad = SileroVad()
    with pytest.raises(ValueError, match="pcm16"):
        vad.prob(bad_frame)
