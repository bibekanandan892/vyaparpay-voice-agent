"""DeepgramStt — the `SttProvider` implementation over Deepgram's streaming
WebSocket listen API (docs/06-voice-pipeline.md §1/§3.2: Nova-3, 16 kHz
mono `linear16` PCM in, partials + finals out).

Transport is a raw `websockets` client, not the Deepgram SDK — the voice
extra pins `websockets` for exactly this (backend/pyproject.toml: "Deepgram/
ElevenLabs streaming websockets"), and no doc mandates an SDK. The API key
rides the `Authorization: Token <key>` handshake header, unwrapped from
`SecretStr` only as a short-lived local inside `stream()` (the same
never-stored, never-logged discipline as app/providers/openrouter.py).

Judgment calls made here, recorded so review argues with the reasoning and
not the diff:

1. **`is_final` vs `speech_final` mapping — the load-bearing decision.**
   Deepgram's two flags are subtly different (pinned from vendor docs):
   `is_final: true` means *this segment's transcript is finalized and will
   not be revised* — but the utterance may continue; interim results that
   follow cover only audio AFTER that segment, so a finalized segment's
   text never reappears in later messages. `speech_final: true` (which
   implies `is_final`) means Deepgram's endpointer heard enough trailing
   silence to call the *utterance* over. Our frozen contract wants
   utterance-level events: `SttPartial` supersedes wholesale and
   `SttFinal` is "the authoritative transcript for one utterance, emitted
   once" (app/domain/voice.py). So this provider accumulates finalized
   segments and emits:
   - `is_final: false`  -> `SttPartial(accumulated segments + interim)`,
   - `is_final: true, speech_final: false` -> fold the segment into the
     accumulator and emit `SttPartial(accumulated)` — a stabilized view,
     deliberately NOT an `SttFinal`,
   - `speech_final: true` -> fold, emit one `SttFinal(accumulated)`,
     reset the accumulator for the next utterance.
2. **`endpointing=250`** so Deepgram's utterance boundary tracks the
   pipeline's own Silero endpoint (docs/06 §5's >= 250 ms trailing-silence
   rule); Deepgram's 10 ms default would fragment one real turn into many
   `speech_final`s. A module constant, not a Settings field, because
   docs/06 §3 names no Deepgram-side tuning knob and config additions are
   additive-only against what the docs name.
3. **`smart_format=true`** because docs/06 §5's completeness hold inspects
   the live partial for terminal punctuation ("ends with terminal
   punctuation") — unpunctuated transcripts would blind that heuristic.
4. **Silence suppression.** Results with empty transcripts emit nothing,
   and a `speech_final` that closes an all-empty accumulator is dropped —
   Deepgram endpoints on silence-only windows routinely (we feed it the
   continuous ingress stream, not VAD-gated audio), and an empty
   `SttFinal` would open an empty turn downstream.
5. **KeepAlive.** Deepgram closes a socket that receives no audio for
   ~10 s (vendor NET-0001 timeout); whenever the audio iterator stalls
   past the keepalive interval (default 5 s) a `{"type": "KeepAlive"}`
   text frame is sent instead. The interval is a constructor knob only so
   tests can shrink it — production callers use the default.
6. **No reconnect logic here.** docs/06 §9 assigns STT-stream-drop
   recovery to the worker ("Worker reconnects the Deepgram stream
   immediately"); an abnormal server close therefore propagates as
   `ConnectionClosedError`, loudly, for the worker to handle.
7. **`ts_ms` = `round((start + duration) * 1000)`** — the media-clock
   position of the END of the transcribed audio, from the `start`/
   `duration` seconds every Results message carries. Matches the frozen
   contract's "worker media-clock milliseconds since stream start",
   because the socket is fed from stream start.
8. **Teardown.** When the audio iterator ends, `{"type": "CloseStream"}`
   is sent (vendor protocol: makes Deepgram flush trailing finals and
   close cleanly); the receive loop then drains the flushed Results until
   the server's clean close ends the stream.

Docs: docs/06-voice-pipeline.md §1/§3.2/§4.1/§9, docs/04-backend-architecture.md
§4 (provider placement/DI). Span note: docs/04 §7.2 assigns the
`stt.final` span to `VoiceAgentWorker`, not to this provider — this module
deliberately opens no spans.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, Final
from urllib.parse import urlencode

import orjson
from pydantic import SecretStr
from websockets.asyncio.client import ClientConnection, connect

from app.config import Settings
from app.domain.voice import SttEvent, SttFinal, SttPartial
from app.obs.logging import get_logger

log = get_logger(__name__)

# Judgment call 2 (docs/06 §5's endpoint-silence value).
_ENDPOINTING_MS: Final = 250
# Judgment call 5 (vendor ~10 s idle timeout; half of it, with margin).
_DEFAULT_KEEPALIVE_INTERVAL_S: Final = 5.0

_MSG_TYPE_RESULTS: Final = "Results"
# Vendor-pinned control frames (Deepgram streaming protocol).
_MSG_KEEP_ALIVE: Final = '{"type": "KeepAlive"}'
_MSG_CLOSE_STREAM: Final = '{"type": "CloseStream"}'


class DeepgramStt:
    """Implements `app.domain.voice.SttProvider` (an async generator, the
    same Protocol spelling caveat as interfaces.py's `LLMProvider.stream`).
    One instance is process-long (built by the voice worker's wiring); one
    `stream()` call is one Deepgram socket for one call's uplink audio."""

    def __init__(
        self,
        settings: Settings,
        *,
        keepalive_interval_s: float = _DEFAULT_KEEPALIVE_INTERVAL_S,
    ) -> None:
        self._require_api_key(settings)  # fail fast at construction, not first turn
        self._settings = settings
        self._keepalive_interval_s = keepalive_interval_s

    @staticmethod
    def _require_api_key(settings: Settings) -> SecretStr:
        key = settings.deepgram_api_key
        if key is None:
            raise ValueError(
                "DEEPGRAM_API_KEY is not configured — DeepgramStt cannot start; "
                "set it in the voice worker's environment (docs/04 §3)."
            )
        return key

    def _listen_url(self, sample_rate: int) -> str:
        """The /v1/listen query string, pinned from docs/06 §3.2 (encoding/
        rate/channels), §1 (model, interim results), and judgment calls
        2-3 (endpointing, smart_format)."""
        params = {
            "model": self._settings.deepgram_model,
            "encoding": "linear16",
            "sample_rate": str(sample_rate),
            "channels": "1",
            "interim_results": "true",
            "smart_format": "true",
            "endpointing": str(_ENDPOINTING_MS),
        }
        return f"{self._settings.deepgram_url}?{urlencode(params)}"

    async def stream(
        self, audio: AsyncIterator[bytes], *, sample_rate: int
    ) -> AsyncIterator[SttEvent]:
        """Feed `audio` (raw 16 kHz s16le PCM byte chunks, frame boundaries
        irrelevant to Deepgram) up the socket while mapping the JSON
        messages coming down into `SttPartial`/`SttFinal` events."""
        headers = {
            # Unwrapped only here, as a request-scoped value — never stored,
            # never logged (openrouter.py's SecretStr discipline).
            "Authorization": f"Token {self._require_api_key(self._settings).get_secret_value()}"
        }
        async with connect(self._listen_url(sample_rate), additional_headers=headers) as ws:
            sender = asyncio.create_task(
                _send_audio(ws, audio, keepalive_interval_s=self._keepalive_interval_s)
            )
            segments: tuple[str, ...] = ()
            try:
                async for raw in ws:
                    events, segments = _map_message(raw, segments)
                    for event in events:
                        yield event
                # Clean server close: if the sender died first (its send
                # raised), surface that instead of swallowing it.
                exc = sender.exception() if sender.done() and not sender.cancelled() else None
                if exc is not None:
                    raise exc
            finally:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)


async def _send_audio(
    ws: ClientConnection, audio: AsyncIterator[bytes], *, keepalive_interval_s: float
) -> None:
    """Pump PCM chunks into the socket as binary frames; KeepAlive when the
    iterator stalls past the interval (judgment call 5); CloseStream once
    it ends (judgment call 8). Uses `asyncio.wait` (which never cancels on
    timeout) rather than `wait_for`, so a keepalive tick can never inject
    a cancellation into the caller's audio generator mid-`__anext__`."""
    pending: asyncio.Task[bytes] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(anext(audio))
            done, _ = await asyncio.wait({pending}, timeout=keepalive_interval_s)
            if not done:
                await ws.send(_MSG_KEEP_ALIVE)
                continue
            finished, pending = pending, None
            try:
                chunk = finished.result()
            except StopAsyncIteration:
                await ws.send(_MSG_CLOSE_STREAM)
                return
            await ws.send(chunk)
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


def _map_message(
    raw: str | bytes, segments: tuple[str, ...]
) -> tuple[list[SttEvent], tuple[str, ...]]:
    """Parse one server message; non-Results types (Metadata, UtteranceEnd,
    SpeechStarted, anything future) are tolerated and skipped — only a
    malformed message fails loudly, with the raw payload logged for
    diagnosis (never the API key; it is not in scope here at all)."""
    try:
        data: Any = orjson.loads(raw)
        if data.get("type") != _MSG_TYPE_RESULTS:
            return [], segments
        text = data["channel"]["alternatives"][0]["transcript"]
        ts_ms = round((data["start"] + data["duration"]) * 1000)
        is_final = bool(data.get("is_final", False))
        speech_final = bool(data.get("speech_final", False))
    except (orjson.JSONDecodeError, KeyError, IndexError, TypeError, AttributeError) as exc:
        log.error(
            "deepgram.stream.malformed_message",
            raw_message=raw if isinstance(raw, str) else raw.decode(errors="replace"),
            error=str(exc),
        )
        raise
    return _fold_result(
        text, ts_ms, is_final=is_final, speech_final=speech_final, segments=segments
    )


def _fold_result(
    text: str, ts_ms: int, *, is_final: bool, speech_final: bool, segments: tuple[str, ...]
) -> tuple[list[SttEvent], tuple[str, ...]]:
    """Judgment call 1's mapping table, as a pure fold over the finalized-
    segment accumulator (returns the new accumulator; never mutates)."""
    text = text.strip()
    if speech_final and not is_final:
        # Deepgram documents speech_final => is_final. If that invariant
        # ever breaks (vendor bug/protocol change), the message falls
        # through to the interim branch and the utterance boundary is
        # dropped — surface it loudly so the live-smoke harness (H1) or
        # production logs catch it instead of a silently hanging turn.
        log.warning("deepgram.stream.speech_final_without_is_final", ts_ms=ts_ms)
    if not is_final:
        if not text:
            return [], segments  # silence interim (judgment call 4)
        return [SttPartial(text=" ".join((*segments, text)), ts_ms=ts_ms)], segments
    merged = (*segments, text) if text else segments
    if not speech_final:
        if not merged:
            return [], merged
        # Finalized segment mid-utterance: a stabilized partial, NOT a final.
        return [SttPartial(text=" ".join(merged), ts_ms=ts_ms)], merged
    if not merged:
        return [], ()  # silence-only utterance: suppressed (judgment call 4)
    return [SttFinal(text=" ".join(merged), ts_ms=ts_ms)], ()


__all__ = ["DeepgramStt"]
