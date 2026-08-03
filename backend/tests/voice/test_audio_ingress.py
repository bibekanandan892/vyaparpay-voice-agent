"""AudioIngress tests (docs/06 §3.2, §7) — all synthetic: real av frames
and raw PCM through the real PyAV resampler, no network, no real time.

Covers: 48 kHz stereo -> 16 kHz mono resample correctness (length +
energy), exact 32 ms VAD hops across ragged input framing, DTX gap ->
inserted silence with honest ts_ms bookkeeping, sub-threshold jitter
absorption, and the seam guards (single subscriber, closed-push,
validation).
"""

from __future__ import annotations

import contextlib
import math
import struct
from fractions import Fraction

import pytest

av = pytest.importorskip("av")

from app.domain.voice import AudioFrame  # noqa: E402
from app.voice.audio_ingress import (  # noqa: E402
    STT_SAMPLE_RATE,
    VAD_HOP_BYTES,
    VAD_HOP_MS,
    VAD_HOP_SAMPLES,
    AudioIngress,
)

SINE_FREQ_HZ = 440.0
SINE_AMPLITUDE = 12_000
# RMS of a full-scale-12000 sine = 12000 / sqrt(2).
SINE_RMS = SINE_AMPLITUDE / math.sqrt(2)


def sine_pcm(rate: int, samples: int, *, channels: int = 1, phase_offset: int = 0) -> bytes:
    """Interleaved s16le sine — identical signal in every channel."""
    out = bytearray()
    for i in range(samples):
        value = int(
            SINE_AMPLITUDE * math.sin(2 * math.pi * SINE_FREQ_HZ * (i + phase_offset) / rate)
        )
        out += struct.pack("<h", value) * channels
    return bytes(out)


def rms(pcm: bytes) -> float:
    values = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    return math.sqrt(sum(v * v for v in values) / max(1, len(values)))


def make_av_frame(pcm: bytes, *, rate: int, channels: int, pts: int | None) -> av.AudioFrame:
    layout = "mono" if channels == 1 else "stereo"
    samples = len(pcm) // (2 * channels)
    frame = av.AudioFrame(format="s16", layout=layout, samples=samples)
    frame.sample_rate = rate
    frame.time_base = Fraction(1, rate)
    if pts is not None:
        frame.pts = pts
    frame.planes[0].update(pcm)
    return frame


async def collect_stt(ingress: AudioIngress) -> bytes:
    chunks: list[bytes] = []
    async with contextlib.aclosing(ingress.stt_stream()) as stream:
        async for chunk in stream:
            chunks.append(chunk)
    return b"".join(chunks)


async def collect_vad(ingress: AudioIngress) -> list[AudioFrame]:
    frames: list[AudioFrame] = []
    async with contextlib.aclosing(ingress.vad_frames()) as stream:
        async for frame in stream:
            frames.append(frame)
    return frames


async def test_resample_48k_stereo_sine_to_16k_mono() -> None:
    # 1 s of 440 Hz at 48 kHz stereo, pushed as 50 contiguous 20 ms wire
    # frames (av-frame seam, honest pts).
    ingress = AudioIngress()
    for k in range(50):
        pcm = sine_pcm(48_000, 960, channels=2, phase_offset=k * 960)
        ingress.push(make_av_frame(pcm, rate=48_000, channels=2, pts=k * 960))
    ingress.close()

    stt = await collect_stt(ingress)
    vad = await collect_vad(ingress)

    # Length: exactly 1 s at 16 kHz mono s16 (3:1 is an exact ratio and
    # close() flushes the resampler tail).
    assert len(stt) == STT_SAMPLE_RATE * 2
    # Energy: the sine's RMS survives downmix + resample within 2%.
    assert rms(stt) == pytest.approx(SINE_RMS, rel=0.02)
    # VAD side: 16000 samples -> 31 whole hops, the 128-sample tail dropped.
    assert len(vad) == STT_SAMPLE_RATE // VAD_HOP_SAMPLES
    assert all(f.samples == VAD_HOP_SAMPLES and len(f.pcm16) == VAD_HOP_BYTES for f in vad)
    assert [f.ts_ms for f in vad] == [i * VAD_HOP_MS for i in range(len(vad))]
    assert all(f.sample_rate == STT_SAMPLE_RATE for f in vad)


async def test_vad_hops_exact_across_ragged_input_frames() -> None:
    # Ragged 16 kHz mono pushes (raw-PCM seam, no pts -> contiguous):
    # hop framing must come out exactly 512 samples / 1024 bytes each.
    sample_counts = [100, 333, 512, 947, 293, 512]
    ingress = AudioIngress()
    offset = 0
    for count in sample_counts:
        pcm = sine_pcm(STT_SAMPLE_RATE, count, phase_offset=offset)
        ingress.push_pcm(pcm, sample_rate=STT_SAMPLE_RATE)
        offset += count
    ingress.close()

    stt = await collect_stt(ingress)
    vad = await collect_vad(ingress)

    total_samples = sum(sample_counts)
    assert len(stt) == total_samples * 2  # STT gets every byte
    assert len(vad) == total_samples // VAD_HOP_SAMPLES  # partial tail dropped
    assert all(len(f.pcm16) == VAD_HOP_BYTES for f in vad)
    assert [f.ts_ms for f in vad] == [i * VAD_HOP_MS for i in range(len(vad))]


async def test_dtx_gap_surfaces_as_silence_with_honest_ts() -> None:
    # 32 ms of speech at t=0, then DTX silence, then 32 ms more at t=544:
    # docs/06 §7 requires the 512 ms gap to arrive as zero PCM so the
    # trailing-silence clock sees real time.
    speech = sine_pcm(STT_SAMPLE_RATE, 512)
    ingress = AudioIngress()
    ingress.push_pcm(speech, sample_rate=STT_SAMPLE_RATE, ts_ms=0)
    ingress.push_pcm(speech, sample_rate=STT_SAMPLE_RATE, ts_ms=544)
    ingress.close()

    stt = await collect_stt(ingress)
    vad = await collect_vad(ingress)

    # 512 speech + 8192 gap-silence + 512 speech samples = 576 ms total.
    assert len(stt) == (512 + 8192 + 512) * 2
    # The inserted region is exactly zero.
    assert stt[512 * 2 : 8704 * 2] == b"\x00" * (8192 * 2)
    # 9216 samples = 18 exact hops with a contiguous media clock.
    assert len(vad) == 18
    assert [f.ts_ms for f in vad] == [i * VAD_HOP_MS for i in range(18)]
    # Hop 0 carries the first burst, hops 1-16 are pure silence, hop 17
    # carries the second burst.
    assert any(b != 0 for b in vad[0].pcm16)
    assert all(f.pcm16 == b"\x00" * VAD_HOP_BYTES for f in vad[1:17])
    assert any(b != 0 for b in vad[17].pcm16)


async def test_sub_threshold_gap_is_absorbed_not_inserted() -> None:
    # A 10 ms pts jump is jitter, not DTX (threshold 40 ms) — no silence.
    speech = sine_pcm(STT_SAMPLE_RATE, 320)
    ingress = AudioIngress()
    ingress.push_pcm(speech, sample_rate=STT_SAMPLE_RATE, ts_ms=0)
    ingress.push_pcm(speech, sample_rate=STT_SAMPLE_RATE, ts_ms=30)
    ingress.close()

    stt = await collect_stt(ingress)
    assert len(stt) == 640 * 2


async def test_each_fanout_allows_exactly_one_subscriber() -> None:
    ingress = AudioIngress()
    ingress.stt_stream()
    ingress.vad_frames()
    with pytest.raises(RuntimeError):
        ingress.stt_stream()
    with pytest.raises(RuntimeError):
        ingress.vad_frames()
    ingress.close()


async def test_push_after_close_raises_and_close_is_idempotent() -> None:
    ingress = AudioIngress()
    ingress.push_pcm(sine_pcm(STT_SAMPLE_RATE, 512), sample_rate=STT_SAMPLE_RATE)
    ingress.close()
    ingress.close()  # idempotent — no double sentinel

    with pytest.raises(RuntimeError):
        ingress.push_pcm(sine_pcm(STT_SAMPLE_RATE, 512), sample_rate=STT_SAMPLE_RATE)

    assert len(await collect_vad(ingress)) == 1


async def test_push_pcm_validates_its_inputs() -> None:
    ingress = AudioIngress()
    with pytest.raises(ValueError):
        ingress.push_pcm(b"\x00", sample_rate=STT_SAMPLE_RATE)  # odd byte count
    with pytest.raises(ValueError):
        ingress.push_pcm(b"", sample_rate=STT_SAMPLE_RATE)  # empty
    with pytest.raises(ValueError):
        ingress.push_pcm(b"\x00\x00", sample_rate=STT_SAMPLE_RATE, channels=3)
    with pytest.raises(ValueError):
        ingress.push_pcm(b"\x00\x00", sample_rate=0)  # not ZeroDivisionError
    with pytest.raises(ValueError):
        ingress.push_pcm(b"\x00\x00", sample_rate=-16_000)
    ingress.close()
