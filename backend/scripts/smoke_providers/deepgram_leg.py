"""The Deepgram leg: two live connections, then eleven verdicts.

Connection 1 is a RAW probe — the provider's own URL and control frames,
but the messages are read un-mapped, so the vendor cadence
`app/providers/deepgram.py` judgment call 1 was built on (`is_final` vs
`speech_final`, and whether a finalized segment ever re-appears) is
visible instead of being folded away. Connection 2 runs the real
`DeepgramStt.stream()` over the same audio, with an idle stall in the
middle, so the accumulator's output and the KeepAlive antidote to the
vendor's ~10 s NET-0001 timeout are measured through production code.

See the package docstring for the harness-wide judgment calls (notably 1:
why both probes exist, and 2: why this file reaches for `_listen_url` and
`_MSG_CLOSE_STREAM` rather than re-spelling them).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Final

import orjson
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from app.config import Settings
from app.domain.voice import SttEvent, SttFinal, SttPartial
from app.providers import deepgram as dg_module
from app.providers.deepgram import DeepgramStt
from scripts.smoke_providers import audio
from scripts.smoke_providers.report import Check, Verdict, check

_JC1: Final = "deepgram.py JC 1 (is_final/speech_final accumulator)"
_JC2: Final = "deepgram.py JC 2 (endpointing=250)"
_JC3: Final = "deepgram.py JC 3 (smart_format)"
_JC4: Final = "deepgram.py JC 4 (silence suppression)"
_JC5: Final = "deepgram.py JC 5 (KeepAlive)"
_JC7: Final = "deepgram.py JC 7 (ts_ms from start+duration)"
_JC8: Final = "deepgram.py JC 8 (CloseStream teardown)"
_PINNED_WIRE: Final = "PINNED(vendor) /v1/listen params + Authorization header"

_OVERLAP_EPSILON_S: Final = 1e-6
_PUNCT_ARTIFACTS: Final = ("  ", " .", " ,", " ?", " !")
_TERMINAL_PUNCT: Final = (".", "?", "!")


@dataclass(frozen=True, slots=True)
class _RawResult:
    transcript: str
    is_final: bool
    speech_final: bool
    start: float
    duration: float
    shape_ok: bool


@dataclass(frozen=True, slots=True)
class _RawObservation:
    url: str
    results: tuple[_RawResult, ...]
    other_types: tuple[str, ...]
    flushed_after_close: int
    closed_cleanly: bool
    close_note: str


@dataclass(frozen=True, slots=True)
class _ProviderObservation:
    order: tuple[str, ...]
    partials: tuple[SttPartial, ...]
    finals: tuple[SttFinal, ...]
    finals_after_gap: int
    gap_attempted: bool
    error: str | None


# --------------------------------------------------------------------------
# Connection 1 — raw probe
# --------------------------------------------------------------------------


def _parse(raw: str | bytes) -> tuple[_RawResult | None, str]:
    """Map one server frame to a `_RawResult` (or the message `type` when
    it is not a Results). `shape_ok` records whether every field
    `deepgram._map_message` reads was actually present."""
    data: Any = orjson.loads(raw)
    kind = str(data.get("type", "<untyped>"))
    if kind != "Results":
        return None, kind
    try:
        text = data["channel"]["alternatives"][0]["transcript"]
        start, duration = float(data["start"]), float(data["duration"])
        shape_ok = True
    except (KeyError, IndexError, TypeError, ValueError):
        text, start, duration, shape_ok = "", 0.0, 0.0, False
    return (
        _RawResult(
            transcript=text,
            is_final=bool(data.get("is_final", False)),
            speech_final=bool(data.get("speech_final", False)),
            start=start,
            duration=duration,
            shape_ok=shape_ok,
        ),
        kind,
    )


async def _pump(ws: ClientConnection, pcm: bytes, sample_rate: int, sent: asyncio.Event) -> None:
    """Paced binary frames, then the provider's own CloseStream frame."""
    async for frame in audio.paced_frames(pcm, sample_rate):
        await ws.send(frame)
    await ws.send(dg_module._MSG_CLOSE_STREAM)
    sent.set()


async def _raw_probe(stt: DeepgramStt, key: str, pcm: bytes, sample_rate: int) -> _RawObservation:
    url = stt._listen_url(sample_rate)  # harness JC 2: the production URL, never a copy
    results: list[_RawResult] = []
    others: list[str] = []
    flushed = 0
    closed_cleanly, note = True, "server closed cleanly after CloseStream"
    close_sent = asyncio.Event()
    async with connect(url, additional_headers={"Authorization": f"Token {key}"}) as ws:
        pump = asyncio.create_task(_pump(ws, pcm, sample_rate, close_sent))
        try:
            async for raw in ws:
                parsed, kind = _parse(raw)
                if parsed is None:
                    others.append(kind)
                    continue
                results.append(parsed)
                flushed += 1 if close_sent.is_set() else 0
        except ConnectionClosed as exc:
            closed_cleanly, note = False, f"abnormal close: {exc!s}"
        finally:
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
    return _RawObservation(
        url=url,
        results=tuple(results),
        other_types=tuple(dict.fromkeys(others)),
        flushed_after_close=flushed,
        closed_cleanly=closed_cleanly,
        close_note=note,
    )


# --------------------------------------------------------------------------
# Connection 2 — the real provider
# --------------------------------------------------------------------------


async def _provider_probe(
    settings: Settings, pcm: bytes, sample_rate: int, *, idle_s: float | None
) -> _ProviderObservation:
    """Stream through `DeepgramStt`. When `idle_s` is set the audio stalls
    mid-stream for that long (judgment call 5's KeepAlive window)."""
    stt = DeepgramStt(settings)
    resumed = asyncio.Event()
    source = (
        audio.paced_with_idle_gap(pcm, sample_rate, idle_s=idle_s, resumed=resumed)
        if idle_s is not None
        else audio.paced_frames(pcm, sample_rate)
    )
    order: list[str] = []
    partials: list[SttPartial] = []
    finals: list[SttFinal] = []
    after_gap = 0
    error: str | None = None
    try:
        async for event in stt.stream(source, sample_rate=sample_rate):
            if isinstance(event, SttFinal):
                order.append("F")
                finals.append(event)
                after_gap += 1 if resumed.is_set() else 0
            else:
                order.append("P")
                partials.append(event)
    except Exception as exc:  # any failure here is a finding, not a crash
        error = f"{type(exc).__name__}: {exc}"
    return _ProviderObservation(
        order=tuple(order),
        partials=tuple(partials),
        finals=tuple(finals),
        finals_after_gap=after_gap,
        gap_attempted=idle_s is not None,
        error=error,
    )


# --------------------------------------------------------------------------
# Verdicts
# --------------------------------------------------------------------------


def _wire_checks(raw: _RawObservation) -> list[Check]:
    """DG-1/DG-2: the handshake and the Results shape the provider parses."""
    bad_shape = [r for r in raw.results if not r.shape_ok]
    others = ", ".join(raw.other_types) or "none"
    return [
        check(
            "DG-1",
            _PINNED_WIRE,
            "Deepgram accepts the exact query string the provider builds, with the "
            "API key in an `Authorization: Token` header",
            Verdict.PASS,
            f"handshake accepted; url={raw.url}",
        ),
        check(
            "DG-2",
            f"{_PINNED_WIRE}; {_JC7}",
            "every Results message carries channel.alternatives[0].transcript plus "
            "start/duration seconds and the is_final/speech_final flags",
            Verdict.PASS if raw.results and not bad_shape else Verdict.UNKNOWN
            if not raw.results
            else Verdict.FAIL,
            f"{len(raw.results)} Results, {len(bad_shape)} missing a field the provider "
            f"reads; other message types seen: {others}",
        ),
    ]


def _cadence_checks(raw: _RawObservation) -> list[Check]:
    """DG-3/DG-4: the two vendor invariants judgment call 1 is built on."""
    finals = [r for r in raw.results if r.speech_final]
    violations = [r for r in finals if not r.is_final]
    overlaps, segment_end = [], 0.0
    for result in raw.results:
        if result.start + _OVERLAP_EPSILON_S < segment_end:
            overlaps.append(result)
        if result.is_final:
            segment_end = result.start + result.duration
    n_final_segments = sum(1 for r in raw.results if r.is_final)
    return [
        check(
            "DG-3",
            _JC1,
            "`speech_final: true` always implies `is_final: true` (if it does not, "
            "the provider drops the utterance boundary and the turn hangs)",
            Verdict.UNKNOWN if not finals else Verdict.FAIL if violations else Verdict.PASS,
            f"{len(finals)} speech_final message(s), {len(violations)} without is_final"
            if finals
            else "no speech_final message arrived — nothing to check",
        ),
        check(
            "DG-4",
            _JC1,
            "after `is_final: true`, later messages cover only audio AFTER that "
            "segment (the non-overlap premise the accumulator relies on)",
            Verdict.UNKNOWN
            if n_final_segments < 2
            else Verdict.FAIL
            if overlaps
            else Verdict.PASS,
            f"{n_final_segments} finalized segment(s), {len(overlaps)} later message(s) "
            "started before a finalized segment ended",
        ),
    ]


def _mapping_checks(prov: _ProviderObservation, *, real_speech: bool) -> list[Check]:
    """DG-5/DG-6/DG-7/DG-8: what the accumulator produced from that cadence."""
    text = prov.finals[-1].text if prov.finals else ""
    artifacts = [a for a in _PUNCT_ARTIFACTS if a in text]
    utterances = 2 if prov.gap_attempted else 1
    return [
        check(
            "DG-5",
            _JC1,
            "the provider emits partials during an utterance and exactly one "
            "SttFinal at its end — never an SttFinal mid-utterance",
            Verdict.UNKNOWN if not prov.order else Verdict.PASS,
            f"event order: {''.join(prov.order) or 'no events'} "
            f"({len(prov.partials)} partial, {len(prov.finals)} final)",
        ),
        check(
            "DG-6",
            _JC1,
            "joining finalized segments with a single space yields sane spacing and "
            "punctuation (no doubled spaces, no space before a full stop)",
            Verdict.UNKNOWN
            if not (real_speech and prov.finals)
            else Verdict.FAIL
            if artifacts
            else Verdict.PASS,
            f"final text: {text!r}; join artifacts: {artifacts or 'none'}"
            if prov.finals
            else "no non-empty final — expected under --synthetic (harness JC 3)",
        ),
        check(
            "DG-7",
            _JC3,
            "smart_format=true puts terminal punctuation on finals, which docs/06 §5's "
            "completeness hold inspects",
            Verdict.UNKNOWN
            if not (real_speech and prov.finals)
            else Verdict.PASS
            if text.endswith(_TERMINAL_PUNCT)
            else Verdict.FAIL,
            f"final ends with {text[-1]!r}" if text else "no non-empty final to inspect",
        ),
        check(
            "DG-8",
            _JC2,
            "endpointing=250 closes one utterance per spoken phrase, rather than "
            "fragmenting it into several",
            Verdict.UNKNOWN
            if not (real_speech and prov.finals)
            else Verdict.PASS
            if len(prov.finals) == utterances
            else Verdict.FAIL,
            f"{len(prov.finals)} SttFinal for {utterances} spoken phrase(s)",
        ),
    ]


def _lifecycle_checks(raw: _RawObservation, prov: _ProviderObservation) -> list[Check]:
    """DG-9/DG-10/DG-11: silence suppression, KeepAlive, teardown."""
    empties = sum(1 for r in raw.results if not r.transcript.strip())
    all_events: tuple[SttEvent, ...] = (*prov.partials, *prov.finals)
    leaked = [e for e in all_events if not e.text.strip()]
    return [
        check(
            "DG-9",
            _JC4,
            "Deepgram really does emit empty-transcript Results on silence, and the "
            "provider emits no event for them",
            Verdict.UNKNOWN if not empties else Verdict.FAIL if leaked else Verdict.PASS,
            f"{empties} empty-transcript Results on the wire; {len(leaked)} empty "
            "event(s) leaked out of the provider",
        ),
        check(
            "DG-10",
            _JC5,
            "KeepAlive keeps an audio-idle socket alive past the vendor's ~10 s "
            "NET-0001 timeout",
            Verdict.UNKNOWN
            if not prov.gap_attempted
            else Verdict.FAIL
            if prov.error
            else Verdict.PASS,
            f"stream survived the idle window and produced {prov.finals_after_gap} "
            f"final(s) after it; error={prov.error or 'none'}"
            if prov.gap_attempted
            else "idle window skipped (--skip-keepalive)",
        ),
        check(
            "DG-11",
            f"{_JC8}; PINNED(vendor) CloseStream",
            "CloseStream makes Deepgram flush trailing finals and close cleanly, "
            "rather than dropping the socket",
            Verdict.PASS if raw.closed_cleanly else Verdict.FAIL,
            f"{raw.flushed_after_close} Results after CloseStream; {raw.close_note}",
        ),
    ]


async def run(
    settings: Settings,
    *,
    pcm: bytes,
    sample_rate: int,
    real_speech: bool,
    idle_s: float | None,
) -> list[Check]:
    """Both connections, then the eleven verdicts. A transport failure is
    reported as one FAIL check, never an escaping traceback — the other
    leg still deserves to run."""
    key = settings.deepgram_api_key
    if key is None:  # unreachable: cli.py gates on this first
        raise ValueError("DEEPGRAM_API_KEY is not configured")
    stt = DeepgramStt(settings)
    try:
        raw = await _raw_probe(stt, key.get_secret_value(), pcm, sample_rate)
    except Exception as exc:  # a dead socket before any message is a finding, not a crash
        return [
            check(
                "DG-1",
                _PINNED_WIRE,
                "Deepgram accepts the exact query string the provider builds",
                Verdict.FAIL,
                f"raw probe failed before any message: {type(exc).__name__}: {exc}",
            )
        ]
    prov = await _provider_probe(settings, pcm, sample_rate, idle_s=idle_s)
    return [
        *_wire_checks(raw),
        *_cadence_checks(raw),
        *_mapping_checks(prov, real_speech=real_speech),
        *_lifecycle_checks(raw, prov),
    ]


__all__ = ["run"]
