"""Real-model smoke tests for app.voice.silero.SileroVad — the ONE test
module that loads the actual pinned ONNX model through onnxruntime.

Coverage is both directions of the discrimination, with no audio binary
in the repo: the negative facts (silence and seeded noise stay below the
activation threshold) are provable with generated frames, and the
positive fact — `prob()` actually crosses 0.5 on speech-like input — is
pinned on a deterministic synthetic voiced phrase (see
`_synthetic_voiced_phrase`). The positive test exists because its
absence let a real defect ship: the module once fed raw 480-sample hops,
which the graph happily *executed* while scoring ~0.0005 on genuine
speech, and every negative-only test stayed green. Whether the model
detects speech *well* is Silero's benchmark; that it detects speech AT
ALL through our integration (tensor names, 512+context input recipe,
state feedback, pcm16 normalization) is ours to prove.

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

from app.voice.silero import (  # noqa: E402
    FRAME_SAMPLES,
    SILERO_MODEL_PATH,
    SileroVad,
)

pytestmark = pytest.mark.skipif(
    not SILERO_MODEL_PATH.exists(),
    reason=(
        "Silero model not fetched — run `python -m scripts.fetch_models` from "
        "backend/ (CI's gates job always fetches it, so these run there)"
    ),
)

SAMPLE_RATE_HZ = 16_000
FRAME_BYTES = FRAME_SAMPLES * 2  # pcm16; FRAME_SAMPLES = 512, the native 32 ms hop
SILENCE_FRAME = bytes(FRAME_BYTES)  # pure digital zeros
ACTIVATION_THRESHOLD = 0.5  # docs/06 §5's default vad_threshold
STABILITY_RUN_FRAMES = 100

# The old, defective hop size this module used to feed (30 ms @ 16 kHz) —
# kept as a named constant so the rejection test reads as the regression
# pin it is.
LEGACY_480_SAMPLE_HOP_BYTES = 480 * 2


def _noise_frames(n: int, seed: int = 1234) -> list[bytes]:
    """Seeded low-level pcm16 noise — deterministic non-constant input
    that exercises the recurrent state without needing speech audio."""
    rng = np.random.default_rng(seed)
    return [
        rng.integers(-2000, 2000, FRAME_SAMPLES, dtype=np.int16).tobytes() for _ in range(n)
    ]


def _synthetic_voiced_phrase() -> bytes:
    """~1.1 s of deterministic speech-like audio (/u a u a/): a sawtooth
    glottal source with declining f0 (115 -> 85 Hz) and seeded jitter,
    shaped by three cascaded time-varying formant resonators gliding
    between the vowels, plus aspiration noise, 60 ms inter-syllable
    closures, and 10 ms voicing ramps.

    Verified empirically against the pinned model at authoring time:
    peak prob ~0.999 and ~80% of frames >= 0.5, stable across seeds —
    well clear of both thresholds asserted in the positive test. A
    static harmonic stack does NOT trigger Silero v6 (max ~0.4 measured
    over many parameter variants); the formant *transitions* are what
    make it read as speech, so keep them if this ever gets tuned.
    """
    sr = SAMPLE_RATE_HZ
    vowels = {"u": (300.0, 870.0, 2240.0), "a": (730.0, 1090.0, 2440.0)}
    seq = ("u", "a", "u", "a")
    syl, clo, glide = int(0.22 * sr), int(0.06 * sr), int(0.04 * sr)
    total = len(seq) * (syl + clo)
    rng = np.random.default_rng(42)

    f0 = np.linspace(115.0, 85.0, total) * (1 + 0.01 * rng.standard_normal(total))
    phase = 2 * np.pi * np.cumsum(f0) / sr
    source = 2 * ((phase / (2 * np.pi)) % 1.0) - 1.0  # sawtooth: rich harmonics
    source += 0.05 * rng.standard_normal(total)  # aspiration noise

    # Formant trajectories: hold each vowel, glide into the next through
    # the closure, lightly smoothed so the resonators do not step.
    tracks = []
    for f_i in range(3):
        track = np.zeros(total)
        for i, v in enumerate(seq):
            s0, s1 = i * (syl + clo), i * (syl + clo) + syl
            track[s0:s1] = vowels[v][f_i]
            if s1 + clo <= total:
                nxt = vowels[seq[(i + 1) % len(seq)]][f_i]
                track[s1 : s1 + clo] = np.linspace(vowels[v][f_i], nxt, clo)
        track = np.convolve(track, np.ones(glide) / glide, mode="same")
        tracks.append(np.clip(track, 200.0, 3500.0))

    out = source
    for track, bw in zip(tracks, (80.0, 120.0, 180.0), strict=True):
        # Time-varying 2nd-order resonator, per-sample coefficients.
        r = np.exp(-np.pi * bw / sr)
        theta = 2 * np.pi * track / sr
        a1, a2 = 2 * r * np.cos(theta), -(r**2)
        gain = 1 - 2 * r * np.cos(theta) + r**2
        y = np.zeros_like(out)
        y1 = y2 = 0.0
        for n in range(total):
            y[n] = gain[n] * out[n] + a1[n] * y1 + a2 * y2
            y2, y1 = y1, y[n]
        out = y

    # Voicing envelope: on during syllables, off during closures.
    env = np.zeros(total)
    ramp = int(0.01 * sr)
    for i in range(len(seq)):
        s0, s1 = i * (syl + clo), i * (syl + clo) + syl
        env[s0:s1] = 1.0
        env[s0 : s0 + ramp] = np.linspace(0, 1, ramp)
        env[s1 - ramp : s1] = np.linspace(1, 0, ramp)
    out = out * env
    out = out / np.max(np.abs(out)) * 0.5
    return (out * 32767).astype(np.int16).tobytes()


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


def test_synthetic_voiced_phrase_crosses_activation_threshold() -> None:
    """The positive half of the discrimination: on a deterministic
    speech-like voiced phrase, `prob()` must actually cross 0.5 — and
    decisively (peak >= 0.8, majority of frames >= 0.5; measured
    headroom is ~0.999 peak / ~80% of frames). The defect this pins
    against scored ~0.0005 on REAL speech while every silence/noise
    test stayed green."""
    vad = SileroVad()
    pcm = _synthetic_voiced_phrase()
    probs = [
        vad.prob(pcm[pos : pos + FRAME_BYTES])
        for pos in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES)
    ]
    assert max(probs) >= 0.8, f"peak prob {max(probs):.4f} — never read as speech"
    frac = sum(p >= ACTIVATION_THRESHOLD for p in probs) / len(probs)
    assert frac >= 0.5, f"only {frac:.0%} of frames >= {ACTIVATION_THRESHOLD}"


def test_prob_is_deterministic_for_identical_input_and_state_sequence() -> None:
    """Two fresh instances fed the same frame sequence produce bit-equal
    probabilities — the CPUExecutionProvider pin plus zero initial state
    and context make the whole recurrence reproducible."""
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
    instance agree exactly (determinism makes this an == check). This
    covers the rolling context too — a stale context would shift the
    576-sample input tensor and change the probability."""
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


@pytest.mark.parametrize(
    "bad_frame",
    [
        b"",
        b"\x00",
        bytes(LEGACY_480_SAMPLE_HOP_BYTES),  # the old defective 30 ms hop
        bytes((FRAME_SAMPLES + 1) * 2),
    ],
)
def test_rejects_frames_that_are_not_exactly_512_samples(bad_frame: bytes) -> None:
    """Silero v6's streaming recipe is exactly 512 samples per hop —
    anything else (notably the 480-sample hop this module once fed)
    fails loudly instead of running the graph on an input shape it
    silently mis-scores (module docstring, judgment call 1)."""
    vad = SileroVad()
    with pytest.raises(ValueError, match="pcm16"):
        vad.prob(bad_frame)
