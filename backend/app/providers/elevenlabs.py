"""ElevenLabsTts — the `TtsProvider` implementation over ElevenLabs'
streaming-input WebSocket synthesis API, yielding 24 kHz mono s16 PCM
`TtsChunk`s with character-timing alignment as they arrive
(docs/06-voice-pipeline.md §3.2/§4.2/§6.3).

Transport is a raw `websockets` client, not the ElevenLabs SDK — the voice
extra pins `websockets` for exactly this (backend/pyproject.toml: "Deepgram/
ElevenLabs streaming websockets"), and no doc mandates an SDK. The API key
rides the `xi-api-key` handshake header, unwrapped from `SecretStr` only as
a short-lived local (the openrouter.py discipline: never stored, never
logged).

Judgment calls made here, recorded so review argues with the reasoning and
not the diff:

1. **WS stream-input, one connection per sentence.** docs/06 §4.2 fixes
   sentence-level dispatch (one synthesize stream per chunker sentence)
   and §6.3 requires character-timing alignment, which the stream-input
   WS delivers on every audio message; the voice extra's `websockets` pin
   settles WS over the HTTP streaming variant. `synthesize()` therefore
   opens `wss://…/v1/text-to-speech/{voice_id}/stream-input`, sends the
   whole sentence, reads until `isFinal`, and closes — per-sentence
   lifecycle, nothing held between sentences.
2. **Message sequence (vendor-pinned):** the opening frame is
   `{"text": " "}` (the protocol's begin-of-stream: text messages must
   always end with a single space), then the sentence as one frame
   `{"text": "<sentence> ", "flush": true}` (`flush` forces immediate
   generation — the §4.1 TTFB line assumes no server-side buffering
   wait), then the end-of-stream frame `{"text": ""}`, after which the
   server streams audio messages and terminates with `isFinal: true`.
3. **Model/format:** `model_id` comes from `settings.elevenlabs_model_id`
   (Flash v2.5, docs/06 §1/§4.1 — a model id is config, never a constant,
   canon §5); `output_format=pcm_24000` is a module constant because the
   24 kHz mono s16 shape is frozen by `TtsChunk`'s contract (docs/06
   §3.2), not a tunable.
4. **Alignment mapping.** Uses the raw `alignment` object (character
   timings against the INPUT text), not `normalizedAlignment` — §6.3's
   truncation maps played milliseconds back to a character offset in the
   generated sentence text, so offsets must index the text we sent. Each
   message's `charStartTimesMs` is treated as relative to that message's
   own audio chunk and rebased to sentence-relative milliseconds by
   adding the exact duration of PCM already yielded (48 bytes/ms at
   24 kHz mono s16); char offsets are rebased by the count of chars seen
   in prior alignment frames. The chunk-relative reading is pinned from
   vendor docs but flagged for live-smoke validation (no live key in this
   environment).
5. **`tts.first_byte` span.** docs/04 §7.2 assigns this span to
   TtsProvider: opened at `synthesize()` entry, ended at the first
   non-empty audio chunk, so the span's own duration IS the TTFB;
   `tts_ttfb_ms` and `sentence_no` attach via `safe_set_attribute`
   (tracing.py's allowlist). A stream that dies before any audio ends the
   span with ERROR status instead.
6. **Retry-on-fallback-voice lives here** — app/domain/voice.py's
   `TtsProvider` docstring places docs/06 §9's mid-sentence
   retry-on-backup-voice policy behind this seam ("callers see one stream
   per sentence either way"). Policy: on a transport/server failure
   BEFORE any audio chunk has been yielded, retry the whole sentence once
   on `settings.elevenlabs_fallback_voice_id` (empty = no fallback).
   After audio has been yielded, the failure propagates instead: the
   Protocol's flat chunk stream has no way to say "resume from ms X", so
   a silent full re-synthesis would double-speak the sentence's already-
   played prefix — the worker's §9 degradation ladder (skip the sentence,
   continue) owns that case.

Docs: docs/06-voice-pipeline.md §3.2/§4/§6.3/§9, docs/04-backend-architecture.md
§4 (provider placement/DI), §7.2 (span table).
"""

from __future__ import annotations

import base64
import time
from collections.abc import AsyncIterator
from typing import Any, Final
from urllib.parse import urlencode

import orjson
from opentelemetry.trace import Status, StatusCode
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from app.config import Settings
from app.domain.voice import TtsChunk
from app.obs.logging import get_logger
from app.obs.tracing import SPAN_TTS_FIRST_BYTE, get_tracer, safe_set_attribute

log = get_logger(__name__)
_tracer = get_tracer(__name__)

# Judgment call 3: the output shape is frozen by TtsChunk's contract.
_OUTPUT_FORMAT: Final = "pcm_24000"
# 24 kHz mono s16 -> 24 samples/ms x 2 bytes/sample.
_PCM_BYTES_PER_MS: Final = 48
# Vendor-pinned protocol frames (judgment call 2).
_MSG_BOS: Final = '{"text": " "}'
_MSG_EOS: Final = '{"text": ""}'


class ElevenLabsError(RuntimeError):
    """An in-band error message from the stream-input socket (the server
    reported failure in a JSON frame rather than a close code)."""


class ElevenLabsTts:
    """Implements `app.domain.voice.TtsProvider` (an async generator, the
    same Protocol spelling caveat as interfaces.py's `LLMProvider.stream`).
    One instance is process-long; one `synthesize()` call is one sentence's
    stream lifecycle (docs/06 §4.2)."""

    def __init__(self, settings: Settings) -> None:
        # Fail fast at construction, not on the first sentence of a live call.
        if settings.elevenlabs_api_key is None:
            raise ValueError(
                "ELEVENLABS_API_KEY is not configured — ElevenLabsTts cannot start; "
                "set it in the voice worker's environment (docs/04 §3)."
            )
        if not settings.elevenlabs_voice_id:
            raise ValueError(
                "ELEVENLABS_VOICE_ID is not configured — ElevenLabsTts needs a primary "
                "voice id (docs/04 §3)."
            )
        self._settings = settings

    def _require_api_key(self) -> str:
        key = self._settings.elevenlabs_api_key
        if key is None:  # unreachable after __init__'s check; kept for mypy + safety
            raise ValueError("ELEVENLABS_API_KEY is not configured")
        return key.get_secret_value()

    def _voice_ids(self) -> tuple[str, ...]:
        """Primary voice, then the configured fallback (judgment call 6);
        empty fallback config means no retry."""
        fallback = self._settings.elevenlabs_fallback_voice_id
        primary = self._settings.elevenlabs_voice_id
        return (primary, fallback) if fallback else (primary,)

    def _stream_input_url(self, voice_id: str) -> str:
        base = self._settings.elevenlabs_url
        for http_prefix, ws_prefix in (("https://", "wss://"), ("http://", "ws://")):
            if base.startswith(http_prefix):
                base = ws_prefix + base.removeprefix(http_prefix)
                break
        query = urlencode(
            {"model_id": self._settings.elevenlabs_model_id, "output_format": _OUTPUT_FORMAT}
        )
        return f"{base}/v1/text-to-speech/{voice_id}/stream-input?{query}"

    async def synthesize(self, text: str, *, sentence_no: int) -> AsyncIterator[TtsChunk]:
        """One sentence in, one stream of `TtsChunk`s out. Fallback-voice
        retry only before the first audio byte (judgment call 6); the
        `tts.first_byte` span brackets exactly the TTFB (judgment call 5)."""
        span = _tracer.start_span(SPAN_TTS_FIRST_BYTE)
        safe_set_attribute(span, "sentence_no", sentence_no)
        started = time.perf_counter()
        first_audio_seen = False
        voice_ids = self._voice_ids()
        try:
            for attempt, voice_id in enumerate(voice_ids):
                try:
                    async for chunk in self._stream_sentence(voice_id, text, sentence_no):
                        if not first_audio_seen and chunk.pcm:
                            ttfb_ms = round((time.perf_counter() - started) * 1000)
                            safe_set_attribute(span, "tts_ttfb_ms", ttfb_ms)
                            span.end()
                            first_audio_seen = True
                        yield chunk
                    return
                except (OSError, WebSocketException, ElevenLabsError) as exc:
                    if first_audio_seen or attempt == len(voice_ids) - 1:
                        raise
                    log.warning(
                        "elevenlabs.synthesize.retry_on_fallback_voice",
                        sentence_no=sentence_no,
                        error=str(exc),
                    )
        finally:
            if not first_audio_seen:
                # No audio byte ever arrived — the TTFB never happened.
                span.set_status(Status(StatusCode.ERROR))
                span.end()

    async def _stream_sentence(
        self, voice_id: str, text: str, sentence_no: int
    ) -> AsyncIterator[TtsChunk]:
        """One WS connection for one sentence (judgment calls 1-2)."""
        # Unwrapped only here, request-scoped — never stored, never logged.
        headers = {"xi-api-key": self._require_api_key()}
        char_base = 0
        ms_base = 0
        async with connect(self._stream_input_url(voice_id), additional_headers=headers) as ws:
            await ws.send(_MSG_BOS)
            await ws.send(orjson.dumps({"text": f"{text} ", "flush": True}).decode())
            await ws.send(_MSG_EOS)
            async for raw in ws:
                data = _decode_message(raw)
                if "error" in data:
                    log.error(
                        "elevenlabs.synthesize.server_error",
                        error=str(data.get("error")),
                        detail=str(data.get("message", "")),
                    )
                    raise ElevenLabsError(
                        f"stream-input error: {data.get('error')}: {data.get('message', '')}"
                    )
                if data.get("isFinal"):
                    return
                chunk = _to_chunk(
                    data, sentence_no=sentence_no, char_base=char_base, ms_base=ms_base
                )
                if chunk is None:
                    continue  # no audio, no alignment: keep-alive/unknown frame, tolerated
                yield chunk
                char_base += len(chunk.alignment)
                ms_base += len(chunk.pcm) // _PCM_BYTES_PER_MS


def _decode_message(raw: str | bytes) -> dict[str, Any]:
    """Every server frame is a JSON object; anything else fails loudly with
    the raw payload logged for diagnosis (never the API key — it is not in
    scope here at all)."""
    try:
        data = orjson.loads(raw)
        if not isinstance(data, dict):
            raise TypeError(f"expected a JSON object, got {type(data).__name__}")
    except (orjson.JSONDecodeError, TypeError) as exc:
        log.error(
            "elevenlabs.synthesize.malformed_message",
            raw_message=raw if isinstance(raw, str) else raw.decode(errors="replace"),
            error=str(exc),
        )
        raise
    return data


def _to_chunk(
    data: dict[str, Any], *, sentence_no: int, char_base: int, ms_base: int
) -> TtsChunk | None:
    """Map one audio message to a `TtsChunk` (judgment call 4). Returns
    None for frames carrying neither audio nor alignment; a pure-audio
    frame gets `alignment=()` (the contract's documented empty case), a
    pure-alignment frame gets `pcm=b""`."""
    audio_b64 = data.get("audio")
    alignment = data.get("alignment")
    if not audio_b64 and not alignment:
        return None
    try:
        pcm = base64.b64decode(audio_b64) if audio_b64 else b""
        pairs: tuple[tuple[int, int], ...] = ()
        if alignment:
            starts = alignment["charStartTimesMs"]
            pairs = tuple((char_base + i, ms_base + int(ms)) for i, ms in enumerate(starts))
    except (KeyError, TypeError, ValueError) as exc:
        log.error("elevenlabs.synthesize.malformed_chunk", error=str(exc))
        raise
    return TtsChunk(pcm=pcm, alignment=pairs, sentence_no=sentence_no)


__all__ = ["ElevenLabsError", "ElevenLabsTts"]
