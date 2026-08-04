"""The ElevenLabs leg: two live connections, then seven verdicts — the
headline one being whether `charStartTimesMs` is chunk-relative or
stream-relative (elevenlabs.py judgment call 4, explicitly flagged there
as an unvalidated guess).

Connection 1 is a RAW probe over the provider's own stream-input URL and
frame constants, reading server messages un-rebased — the raw numbers a
human needs to see the truth at a glance. Connection 2 runs the real
`ElevenLabsTts.synthesize()` over the same sentence, measuring TTFB
against docs/06 §4.1's target and checking whether the provider's
alignment sanity ceiling (elevenlabs.py's fail-loud guard on judgment
call 4) ever fires.

See the package docstring for the harness-wide judgment calls (notably 2:
why this file reaches for `_stream_input_url`/`_MSG_BOS`/`_MSG_EOS`
rather than re-spelling the wire).
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Any, Final

import orjson
from websockets.asyncio.client import connect

from app.config import Settings
from app.providers import elevenlabs as el_module
from app.providers.elevenlabs import ElevenLabsError, ElevenLabsTts
from scripts.smoke_providers.report import Check, Verdict, check

_JC1: Final = "elevenlabs.py JC 1 (one WS connection per sentence)"
_JC2: Final = "elevenlabs.py JC 2 (BOS/sentence+flush/EOS frame sequence)"
_JC3: Final = "elevenlabs.py JC 3 (output_format=pcm_24000)"
_JC4: Final = "elevenlabs.py JC 4 (charStartTimesMs chunk-relative rebasing + ceiling)"
_JC5: Final = "elevenlabs.py JC 5 (tts.first_byte span brackets TTFB)"
_PINNED_WIRE: Final = "PINNED(vendor) stream-input path + model_id/output_format + xi-api-key"

# docs/06-voice-pipeline.md §4.1: ElevenLabs Flash TTFB p50/p95.
_TTFB_P50_TARGET_MS: Final = 120
_TTFB_P95_TARGET_MS: Final = 250

# A short, cheap sentence — this is the entire billed synthesis for the leg
# (cli.py's cost banner covers the estimate, harness judgment call 5 there).
DEFAULT_SENTENCE: Final = "This is a short live smoke test of the ElevenLabs voice."

# Below this, a message's own audio is too short to distinguish
# chunk-relative from stream-relative by ratio alone — skip it as a vote.
_MIN_CUMULATIVE_MS_FOR_VOTE: Final = 5


@dataclass(frozen=True, slots=True)
class _RawAlignmentSample:
    message_no: int
    cumulative_pcm_ms_before: int
    own_pcm_ms: int
    raw_first_ms: int
    raw_last_ms: int


@dataclass(frozen=True, slots=True)
class _RawObservation:
    url: str
    samples: tuple[_RawAlignmentSample, ...]
    saw_audio: bool
    saw_alignment: bool
    saw_normalized_alignment: bool
    ended_with_is_final: bool
    total_pcm_ms: int


@dataclass(frozen=True, slots=True)
class _ProviderObservation:
    ttfb_ms: int | None
    chunk_count: int
    total_pcm_bytes: int
    alignment_pairs: tuple[tuple[int, int], ...]
    ceiling_error: str | None
    other_error: str | None


# --------------------------------------------------------------------------
# Connection 1 — raw probe
# --------------------------------------------------------------------------


async def _raw_probe(tts: ElevenLabsTts, voice_id: str, key: str, text: str) -> _RawObservation:
    url = tts._stream_input_url(voice_id)  # harness JC 2: the production URL builder
    samples: list[_RawAlignmentSample] = []
    saw_audio = saw_alignment = saw_normalized = ended_final = False
    cumulative_ms = 0
    message_no = 0
    async with connect(url, additional_headers={"xi-api-key": key}) as ws:
        await ws.send(el_module._MSG_BOS)
        await ws.send(orjson.dumps({"text": f"{text} ", "flush": True}).decode())
        await ws.send(el_module._MSG_EOS)
        async for raw in ws:
            data: Any = orjson.loads(raw)
            if data.get("isFinal"):
                ended_final = True
                break
            own_ms = 0
            audio_b64 = data.get("audio")
            if audio_b64:
                saw_audio = True
                own_ms = len(base64.b64decode(audio_b64)) // el_module._PCM_BYTES_PER_MS
            alignment = data.get("alignment")
            if alignment:
                saw_alignment = True
                starts = alignment.get("charStartTimesMs") or []
                if starts:
                    samples.append(
                        _RawAlignmentSample(
                            message_no=message_no,
                            cumulative_pcm_ms_before=cumulative_ms,
                            own_pcm_ms=own_ms,
                            raw_first_ms=int(starts[0]),
                            raw_last_ms=int(starts[-1]),
                        )
                    )
            if data.get("normalizedAlignment"):
                saw_normalized = True
            cumulative_ms += own_ms
            message_no += 1
    return _RawObservation(
        url=url,
        samples=tuple(samples),
        saw_audio=saw_audio,
        saw_alignment=saw_alignment,
        saw_normalized_alignment=saw_normalized,
        ended_with_is_final=ended_final,
        total_pcm_ms=cumulative_ms,
    )


# --------------------------------------------------------------------------
# Connection 2 — the real provider
# --------------------------------------------------------------------------


async def _provider_probe(settings: Settings, text: str) -> _ProviderObservation:
    tts = ElevenLabsTts(settings)
    started = time.perf_counter()
    ttfb_ms: int | None = None
    chunk_count = 0
    total_bytes = 0
    alignment: list[tuple[int, int]] = []
    ceiling_error: str | None = None
    other_error: str | None = None
    try:
        async for chunk in tts.synthesize(text, sentence_no=0):
            chunk_count += 1
            if ttfb_ms is None and chunk.pcm:
                ttfb_ms = round((time.perf_counter() - started) * 1000)
            total_bytes += len(chunk.pcm)
            alignment.extend(chunk.alignment)
    except ElevenLabsError as exc:
        if "alignment timestamps exceed" in str(exc):
            ceiling_error = str(exc)
        else:
            other_error = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # a dead socket mid-stream is a finding, not a crash
        other_error = f"{type(exc).__name__}: {exc}"
    return _ProviderObservation(
        ttfb_ms=ttfb_ms,
        chunk_count=chunk_count,
        total_pcm_bytes=total_bytes,
        alignment_pairs=tuple(alignment),
        ceiling_error=ceiling_error,
        other_error=other_error,
    )


# --------------------------------------------------------------------------
# Verdicts
# --------------------------------------------------------------------------


def _classify_relativity(samples: tuple[_RawAlignmentSample, ...]) -> tuple[str, str]:
    """The headline question. Returns (verdict_word, evidence_line) with
    the raw un-rebased numbers always included in the evidence — a human
    should be able to see the truth at a glance, not just trust the
    classifier. Only samples past the first are informative: the first
    alignment message's cumulative-before is 0, where both hypotheses
    coincide."""
    table = "; ".join(
        f"msg#{s.message_no} cum_before={s.cumulative_pcm_ms_before}ms "
        f"own={s.own_pcm_ms}ms raw=[{s.raw_first_ms}..{s.raw_last_ms}]ms"
        for s in samples
    )
    votes = [s for s in samples[1:] if s.cumulative_pcm_ms_before >= _MIN_CUMULATIVE_MS_FOR_VOTE]
    if not votes:
        return "inconclusive", f"only {len(samples)} alignment message(s) — raw: {table or 'none'}"
    chunk_votes = sum(1 for s in votes if s.raw_last_ms <= s.cumulative_pcm_ms_before * 0.5)
    stream_votes = sum(
        1 for s in votes if abs(s.raw_last_ms - s.cumulative_pcm_ms_before) <= s.own_pcm_ms + 50
    )
    if chunk_votes > stream_votes:
        verdict_word = "chunk-relative"
    elif stream_votes > chunk_votes:
        verdict_word = "stream-relative"
    else:
        verdict_word = "inconclusive"
    return (
        verdict_word,
        f"{chunk_votes} chunk-relative vote(s), {stream_votes} stream-relative "
        f"vote(s) out of {len(votes)} — raw: {table}",
    )


def _wire_checks(raw: _RawObservation) -> list[Check]:
    """EL-1/EL-2/EL-3: the handshake, the frame sequence, the decoy field."""
    return [
        check(
            "EL-1",
            f"{_PINNED_WIRE}; {_JC1}",
            "ElevenLabs accepts the stream-input path with model_id/output_format "
            "query params and the xi-api-key handshake header",
            Verdict.PASS,
            f"handshake accepted; url={raw.url}",
        ),
        check(
            "EL-2",
            f"{_PINNED_WIRE}; {_JC2}",
            "the BOS / sentence+flush / EOS sequence gets audio and/or alignment "
            "back, terminated by isFinal",
            Verdict.PASS
            if raw.ended_with_is_final and (raw.saw_audio or raw.saw_alignment)
            else Verdict.FAIL,
            f"saw_audio={raw.saw_audio} saw_alignment={raw.saw_alignment} "
            f"ended_with_isFinal={raw.ended_with_is_final}",
        ),
        check(
            "EL-3",
            _PINNED_WIRE,
            "server messages carry a normalizedAlignment alongside alignment "
            "(the decoy the provider must NOT read, per JC 4)",
            Verdict.PASS if raw.saw_normalized_alignment else Verdict.UNKNOWN,
            f"normalizedAlignment present={raw.saw_normalized_alignment}",
        ),
    ]


def _relativity_check(raw: _RawObservation) -> Check:
    """EL-4: the headline check — classified from the raw wire numbers."""
    verdict_word, evidence = _classify_relativity(raw.samples)
    verdict = {
        "chunk-relative": Verdict.PASS,
        "stream-relative": Verdict.FAIL,
        "inconclusive": Verdict.UNKNOWN,
    }[verdict_word]
    return check(
        "EL-4",
        _JC4,
        "charStartTimesMs is chunk-relative (resets near 0 each message), NOT "
        "stream-relative (continues counting from stream start) — the pinned "
        "vendor reading the rebasing math depends on",
        verdict,
        f"classified as {verdict_word}; {evidence}",
    )


def _ceiling_check(prov: _ProviderObservation) -> Check:
    """EL-5: does the real provider's fail-loud ceiling on JC 4 ever fire?"""
    if prov.ceiling_error:
        verdict, detail = (
            Verdict.FAIL,
            f"the provider's fail-loud sanity ceiling fired: {prov.ceiling_error} — "
            "this is direct evidence the chunk-relative guess is WRONG",
        )
    elif prov.other_error:
        verdict = Verdict.UNKNOWN
        detail = f"stream failed for an unrelated reason: {prov.other_error}"
    elif not prov.alignment_pairs:
        verdict, detail = Verdict.UNKNOWN, "no alignment pairs were yielded to check"
    else:
        verdict, detail = (
            Verdict.PASS,
            f"stream completed with {len(prov.alignment_pairs)} rebased alignment "
            f"pair(s) and the ceiling never fired: {prov.alignment_pairs}",
        )
    return check(
        "EL-5",
        _JC4,
        "ElevenLabsTts.synthesize() completes without tripping its own "
        "alignment-sanity ceiling (elevenlabs.py's fail-loud guard on JC 4)",
        verdict,
        detail,
    )


def _ttfb_check(prov: _ProviderObservation) -> Check:
    """EL-6: measured TTFB against docs/06 §4.1's target."""
    claim = (
        f"TTFB is within docs/06 §4.1's target (p50 {_TTFB_P50_TARGET_MS} ms, "
        f"p95 {_TTFB_P95_TARGET_MS} ms) — single sample here, not a real percentile"
    )
    if prov.ttfb_ms is None:
        return check(
            "EL-6",
            _JC5,
            claim,
            Verdict.UNKNOWN,
            f"no audio byte was ever received; other_error={prov.other_error or 'none'}",
        )
    verdict = Verdict.PASS if prov.ttfb_ms <= _TTFB_P95_TARGET_MS else Verdict.FAIL
    note = "at/under p50" if prov.ttfb_ms <= _TTFB_P50_TARGET_MS else "over p50, within p95"
    return check(
        "EL-6",
        _JC5,
        claim,
        verdict,
        f"measured {prov.ttfb_ms} ms ({note if verdict is Verdict.PASS else 'over p95'})",
    )


def _format_check(prov: _ProviderObservation) -> Check:
    """EL-7: output shape sanity (judgment call 3)."""
    duration_s = prov.total_pcm_bytes / (24_000 * 2) if prov.total_pcm_bytes else 0.0
    sane = prov.total_pcm_bytes > 0 and prov.total_pcm_bytes % 2 == 0
    return check(
        "EL-7",
        _JC3,
        "output_format=pcm_24000 yields non-empty, even-byte-count s16 PCM whose "
        "decoded duration is a plausible length for the sentence synthesized",
        Verdict.PASS if sane else Verdict.FAIL,
        f"{prov.total_pcm_bytes} PCM bytes across {prov.chunk_count} chunk(s) "
        f"(~{duration_s:.2f} s at 24 kHz mono s16)",
    )


async def run(settings: Settings, *, text: str = DEFAULT_SENTENCE) -> list[Check]:
    """Both connections, then the seven verdicts. A transport failure on
    the raw probe is reported as one FAIL check, never an escaping
    traceback — the provider probe still deserves to run."""
    key = settings.elevenlabs_api_key
    if key is None:  # unreachable: cli.py gates on this first
        raise ValueError("ELEVENLABS_API_KEY is not configured")
    tts = ElevenLabsTts(settings)
    voice_id = settings.elevenlabs_voice_id
    try:
        raw = await _raw_probe(tts, voice_id, key.get_secret_value(), text)
    except Exception as exc:  # a dead socket before any message is a finding, not a crash
        return [
            check(
                "EL-1",
                f"{_PINNED_WIRE}; {_JC1}",
                "ElevenLabs accepts the stream-input path with the provider's "
                "query params and handshake header",
                Verdict.FAIL,
                f"raw probe failed before any message: {type(exc).__name__}: {exc}",
            )
        ]
    prov = await _provider_probe(settings, text)
    return [
        *_wire_checks(raw),
        _relativity_check(raw),
        _ceiling_check(prov),
        _ttfb_check(prov),
        _format_check(prov),
    ]


__all__ = ["DEFAULT_SENTENCE", "run"]
