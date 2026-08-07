"""DeepgramTts — a second `TtsProvider` implementation, over Deepgram's
Aura REST synthesis endpoint, yielding 24 kHz mono s16 PCM `TtsChunk`s as
the chunked HTTP response streams in.

Why this exists: ElevenLabs gates API access behind a paid credit balance,
while the Deepgram account this project already holds for STT covers Aura
TTS under the same $200 free credit — so the demo's zero-cost constraint
is met with a key that is already configured, not a new account. Selected
via `settings.tts_provider` ("elevenlabs" stays the default; nothing
changes for existing deployments).

Unlike elevenlabs.py, whose wire assumptions are pinned from vendor docs
and still await live validation (H1), every wire behavior here was
**validated live against the real endpoint with a real key before this
file was written** (2026-08-07): `Authorization: Token <key>` accepted,
`encoding=linear16&sample_rate=24000&container=none` returns
`audio/l16;rate=24000` chunked raw PCM, and the decoded samples are real
speech (near-zero mean, healthy peak amplitude).

Judgment calls, recorded so review argues with the reasoning:

1. **REST, not Deepgram's WS speak endpoint.** The pipeline dispatches one
   `synthesize()` per chunker sentence (docs/06 §4.2) with the full
   sentence text known up front — the WS endpoint's incremental-text
   feeding buys nothing here, and the REST stream still chunks audio out
   as it synthesizes (183 chunks observed on the live smoke).
2. **No character alignment — and that is a designed-for case.**
   Aura provides no character-timing data, so every chunk carries
   `alignment=()` (the `TtsChunk` contract's documented empty case).
   Barge-in truncation then degrades to proportional estimation —
   truncation.py's own judgment call 3 spells this exact path out. Less
   precise truncation on interruption, never a wrong pipeline.
3. **One process-long `httpx.AsyncClient`, owned here.** A per-sentence
   client would pay a TLS handshake per sentence (the dominant cost in
   the observed 1.7 s cold TTFB); pooling makes later sentences cheap.
   Owned rather than injected because `run.py`'s `_lazy_call_deps` builds
   providers in a sync context with no shared client in scope and no
   teardown seam — the same "one instance is process-long" lifecycle
   ElevenLabsTts documents. `aclose()` exists for tests and any future
   shutdown seam.
4. **No fallback-voice retry.** That policy (elevenlabs.py judgment
   call 6) exists because ElevenLabs voices are account-scoped resources
   that can independently 404; an Aura model name is a platform constant.
   A transport/server failure propagates: before the first byte the
   worker's §9 degradation ladder (skip the sentence, continue) owns it —
   the same after-first-byte posture ElevenLabs takes, applied uniformly.
5. **`tts.first_byte` span** — identical contract to elevenlabs.py
   judgment call 5: opened at entry, ended at first non-empty chunk with
   `tts_ttfb_ms`; ERROR status if the stream dies first; a clean
   zero-audio completion ends OK.
6. **Cost accounting needs no code change.** speech.py bills
   `len(sentence.text)` at dispatch through `CostTracker.record_tts_text`
   against `settings.tts_usd_per_mchar` — provider-agnostic by
   construction. Deploys switching to Aura set `TTS_USD_PER_MCHAR=30`
   (Aura-2's $0.030/1k chars) alongside `TTS_PROVIDER=deepgram`.

Docs: docs/06-voice-pipeline.md §3.2/§4/§9, docs/04-backend-architecture.md
§4 (provider placement), §7.2 (span table).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

import httpx
from opentelemetry.trace import Status, StatusCode

from app.config import Settings
from app.domain.voice import TtsChunk
from app.obs.logging import get_logger
from app.obs.tracing import SPAN_TTS_FIRST_BYTE, get_tracer, safe_set_attribute

log = get_logger(__name__)
_tracer = get_tracer(__name__)

# The output shape is frozen by TtsChunk's contract (24 kHz mono s16),
# same reasoning as elevenlabs.py's _OUTPUT_FORMAT constant.
_ENCODING_PARAMS = {"encoding": "linear16", "sample_rate": "24000", "container": "none"}
# Bounded error-body read: enough for any Deepgram error shape, never a
# whole mis-routed audio body.
_MAX_ERROR_BODY_BYTES = 4 * 1024


class DeepgramTtsError(RuntimeError):
    """A non-2xx response from the speak endpoint."""


class DeepgramTts:
    """Implements `app.domain.voice.TtsProvider` (async generator — the
    same Protocol spelling caveat as interfaces.py's `LLMProvider.stream`).
    One instance is process-long; one `synthesize()` call is one
    sentence's stream lifecycle (docs/06 §4.2)."""

    def __init__(self, settings: Settings) -> None:
        # Fail fast at construction, not on the first sentence of a live
        # call — same posture as ElevenLabsTts, and run.py's lazy-deps
        # judgment call 1 depends on it ("a missing voice key fails THAT
        # call loudly").
        if settings.deepgram_api_key is None:
            raise ValueError(
                "DEEPGRAM_API_KEY is not configured — DeepgramTts cannot start; "
                "set it in the voice worker's environment (docs/04 §3)."
            )
        self._settings = settings
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))

    async def aclose(self) -> None:
        await self._http.aclose()

    def _require_api_key(self) -> str:
        key = self._settings.deepgram_api_key
        if key is None:  # unreachable after __init__'s check; kept for mypy + safety
            raise ValueError("DEEPGRAM_API_KEY is not configured")
        return key.get_secret_value()

    async def synthesize(self, text: str, *, sentence_no: int) -> AsyncIterator[TtsChunk]:
        """One sentence in, one stream of `TtsChunk`s out (judgment
        calls 1-2); the `tts.first_byte` span brackets exactly the TTFB
        (judgment call 5)."""
        span = _tracer.start_span(SPAN_TTS_FIRST_BYTE)
        safe_set_attribute(span, "sentence_no", sentence_no)
        started = time.perf_counter()
        span_ended = False
        try:
            async with self._http.stream(
                "POST",
                self._settings.deepgram_speak_url,
                params={"model": self._settings.deepgram_tts_model, **_ENCODING_PARAMS},
                # Unwrapped request-scoped, never stored or logged — the
                # openrouter.py discipline.
                headers={"Authorization": f"Token {self._require_api_key()}"},
                json={"text": text},
            ) as response:
                if response.status_code != httpx.codes.OK:
                    body = (await response.aread())[:_MAX_ERROR_BODY_BYTES]
                    detail = body.decode(errors="replace")
                    log.error(
                        "deepgram_tts.synthesize.http_error",
                        status=response.status_code,
                        detail=detail,
                        sentence_no=sentence_no,
                    )
                    raise DeepgramTtsError(
                        f"speak endpoint returned {response.status_code}: {detail}"
                    )
                async for pcm in response.aiter_bytes():
                    if not pcm:
                        continue
                    if not span_ended:
                        ttfb_ms = round((time.perf_counter() - started) * 1000)
                        safe_set_attribute(span, "tts_ttfb_ms", ttfb_ms)
                        span.end()
                        span_ended = True
                    yield TtsChunk(pcm=pcm, alignment=(), sentence_no=sentence_no)
            if not span_ended:
                # Clean zero-audio completion — nothing failed, no TTFB to
                # record; end OK so the docs/04 §7.3 error-rate panel sees
                # no false ERROR (elevenlabs.py's identical posture).
                span.end()
                span_ended = True
        finally:
            if not span_ended:
                # Exception (or consumer close) before any audio byte —
                # the TTFB genuinely never happened.
                span.set_status(Status(StatusCode.ERROR))
                span.end()


__all__ = ["DeepgramTts", "DeepgramTtsError"]
