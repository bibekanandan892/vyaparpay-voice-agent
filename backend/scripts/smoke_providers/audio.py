"""Audio sources for the Deepgram leg, and the real-time pacer both legs
feed sockets through.

`synthetic_voiced_phrase()` reimplements the technique proven in
`tests/voice/test_silero.py::_synthetic_voiced_phrase` (importing from
`tests/` is not allowed, so this is a compact re-derivation, not a copy):
a sawtooth glottal source with declining f0 and seeded jitter, shaped by
three cascaded time-varying formant resonators gliding between /u/ and
/a/, with inter-syllable closures and voicing ramps. That test module
records the empirical finding this rests on — a STATIC harmonic stack
does not read as speech to Silero v6 (~0.4 peak), the formant
*transitions* are what carry it (~0.999 peak, ~80% of frames over the
0.5 activation threshold) — so if this is ever tuned, keep the glides.

It is speech-LIKE, not speech: no listener and no ASR will find words in
`/u a u a/`. Harness judgment call 3 is the consequence — every
transcript-dependent check reports UNKNOWN under `--synthetic`.
"""

from __future__ import annotations

import asyncio
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Final

import numpy as np
from numpy.typing import NDArray

SAMPLE_RATE_HZ: Final = 16_000
BYTES_PER_SAMPLE: Final = 2  # s16le mono, the shape docs/06 §3.2 fixes for Deepgram

_FRAME_MS: Final = 20  # the wire's own frame size (docs/06 §1: Opus 20 ms)
# Formant triples (F1, F2, F3) and the syllable sequence, from the
# test_silero.py phrase this is derived from.
_VOWELS: Final = {"u": (300.0, 870.0, 2240.0), "a": (730.0, 1090.0, 2440.0)}
_SEQ: Final = ("u", "a", "u", "a")
_BANDWIDTHS: Final = (80.0, 120.0, 180.0)
_SYLLABLE_S: Final = 0.22
_CLOSURE_S: Final = 0.06
_GLIDE_S: Final = 0.04
_RAMP_S: Final = 0.01


def _formant_tracks(total: int, syl: int, clo: int, glide: int) -> list[NDArray[np.float64]]:
    """Per-sample F1/F2/F3 trajectories: hold each vowel for its syllable,
    glide into the next through the closure, lightly smoothed so the
    resonators do not step."""
    tracks = []
    for f_i in range(3):
        track = np.zeros(total)
        for i, vowel in enumerate(_SEQ):
            s0, s1 = i * (syl + clo), i * (syl + clo) + syl
            track[s0:s1] = _VOWELS[vowel][f_i]
            if s1 + clo <= total:
                nxt = _VOWELS[_SEQ[(i + 1) % len(_SEQ)]][f_i]
                track[s1 : s1 + clo] = np.linspace(_VOWELS[vowel][f_i], nxt, clo)
        track = np.convolve(track, np.ones(glide) / glide, mode="same")
        tracks.append(np.clip(track, 200.0, 3500.0))
    return tracks


def _resonate(
    source: NDArray[np.float64], tracks: list[NDArray[np.float64]], sample_rate: int
) -> NDArray[np.float64]:
    """Cascade three 2nd-order resonators with per-sample coefficients."""
    out = source
    for track, bandwidth in zip(tracks, _BANDWIDTHS, strict=True):
        r = float(np.exp(-np.pi * bandwidth / sample_rate))
        theta = 2 * np.pi * track / sample_rate
        a1, a2 = 2 * r * np.cos(theta), -(r**2)
        gain = 1 - 2 * r * np.cos(theta) + r**2
        y = np.zeros_like(out)
        y1: float = 0.0
        y2: float = 0.0
        for n in range(out.size):
            y[n] = gain[n] * out[n] + a1[n] * y1 + a2 * y2
            y2, y1 = y1, float(y[n])
        out = y
    return out


def _voicing_envelope(total: int, syl: int, clo: int, ramp: int) -> NDArray[np.float64]:
    """On during syllables (with 10 ms ramps), off during closures."""
    env = np.zeros(total)
    for i in range(len(_SEQ)):
        s0, s1 = i * (syl + clo), i * (syl + clo) + syl
        env[s0:s1] = 1.0
        env[s0 : s0 + ramp] = np.linspace(0, 1, ramp)
        env[s1 - ramp : s1] = np.linspace(1, 0, ramp)
    return env


def synthetic_voiced_phrase(sample_rate: int = SAMPLE_RATE_HZ, seed: int = 42) -> bytes:
    """~1.1 s of deterministic speech-like s16le mono PCM (/u a u a/)."""
    syl, clo = int(_SYLLABLE_S * sample_rate), int(_CLOSURE_S * sample_rate)
    glide, ramp = int(_GLIDE_S * sample_rate), int(_RAMP_S * sample_rate)
    total = len(_SEQ) * (syl + clo)
    rng = np.random.default_rng(seed)

    f0 = np.linspace(115.0, 85.0, total) * (1 + 0.01 * rng.standard_normal(total))
    phase = 2 * np.pi * np.cumsum(f0) / sample_rate
    source = 2 * ((phase / (2 * np.pi)) % 1.0) - 1.0  # sawtooth: rich harmonics
    source += 0.05 * rng.standard_normal(total)  # aspiration noise

    out = _resonate(source, _formant_tracks(total, syl, clo, glide), sample_rate)
    out = out * _voicing_envelope(total, syl, clo, ramp)
    out = out / np.max(np.abs(out)) * 0.5
    return bytes(np.asarray(out * 32767, dtype=np.int16).tobytes())


def silence(duration_s: float, sample_rate: int = SAMPLE_RATE_HZ) -> bytes:
    """Digital-zero PCM. Deepgram's endpointer needs trailing silence to
    call an utterance over (`endpointing=250`, deepgram.py judgment 2)."""
    return bytes(int(duration_s * sample_rate) * BYTES_PER_SAMPLE)


def load_wav(path: Path) -> tuple[bytes, int]:
    """Read a mono 16-bit PCM WAV into (pcm, sample_rate). No resampling
    and no format coercion — `DeepgramStt.stream()` takes the rate as a
    parameter, so the honest move is to pass the file's own rate through
    and refuse anything that is not already the s16le mono `linear16`
    shape the provider claims to send."""
    with wave.open(str(path), "rb") as wav:
        channels, width, rate = wav.getnchannels(), wav.getsampwidth(), wav.getframerate()
        if channels != 1 or width != BYTES_PER_SAMPLE:
            raise ValueError(
                f"{path}: need mono 16-bit PCM (got {channels} channel(s), "
                f"{width * 8}-bit) — convert first, e.g. "
                f"`ffmpeg -i {path.name} -ac 1 -ar 16000 -c:a pcm_s16le out.wav`"
            )
        return wav.readframes(wav.getnframes()), rate


def duration_s(pcm: bytes, sample_rate: int) -> float:
    return len(pcm) / (sample_rate * BYTES_PER_SAMPLE)


async def paced_frames(pcm: bytes, sample_rate: int) -> AsyncIterator[bytes]:
    """Yield 20 ms frames one wall-clock 20 ms apart (harness judgment
    call 4: a burst would hand the endpointer a cadence no live call ever
    produces)."""
    frame_bytes = int(sample_rate * _FRAME_MS / 1000) * BYTES_PER_SAMPLE
    for start in range(0, len(pcm), frame_bytes):
        yield pcm[start : start + frame_bytes]
        await asyncio.sleep(_FRAME_MS / 1000)


async def paced_with_idle_gap(
    pcm: bytes, sample_rate: int, *, idle_s: float, resumed: asyncio.Event
) -> AsyncIterator[bytes]:
    """The phrase, then a stall during which NO audio and no bytes of any
    kind leave this iterator, then the phrase again. The stall is what
    Deepgram's ~10 s NET-0001 idle timeout kills, and what
    `deepgram._send_audio`'s KeepAlive is supposed to survive (judgment
    call 5). `resumed` is set the moment the stall ends, so the caller can
    attribute later events to the post-idle half of the stream."""
    async for frame in paced_frames(pcm, sample_rate):
        yield frame
    await asyncio.sleep(idle_s)
    resumed.set()
    async for frame in paced_frames(pcm, sample_rate):
        yield frame


__all__ = [
    "BYTES_PER_SAMPLE",
    "SAMPLE_RATE_HZ",
    "duration_s",
    "load_wav",
    "paced_frames",
    "paced_with_idle_gap",
    "silence",
    "synthetic_voiced_phrase",
]
