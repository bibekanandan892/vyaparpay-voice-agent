"""Wire-level tests for `DeepgramTts.synthesize()` against respx-mocked
httpx (docs/04 §9.4's provider-mocking rule — same pattern as
test_openrouter.py, not the WS fake elevenlabs needs, because this
provider is plain REST).

Unlike test_elevenlabs.py's PINNED(vendor) markers, nothing here is an
unvalidated pin: every wire behavior asserted below was validated against
the live speak endpoint with a real key on 2026-08-07 (see the provider's
module docstring) — `Token` auth, the linear16/24000/none params, chunked
raw-PCM streaming.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import orjson
import pytest
import respx
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

import app.providers.deepgram_tts as deepgram_tts_module
from app.config import Settings
from app.domain.voice import TtsChunk
from app.providers.deepgram_tts import DeepgramTts, DeepgramTtsError
from tests.conftest import SettingsFactory

_API_KEY = "test-dg-key"
_SPEAK_URL = "https://deepgram.test/v1/speak"
_TIMEOUT_S = 10.0


def _settings(settings_factory: SettingsFactory, **overrides: object) -> Settings:
    return settings_factory(
        deepgram_api_key=_API_KEY,
        deepgram_speak_url=_SPEAK_URL,
        **overrides,
    )


@pytest.fixture
async def make_tts() -> AsyncIterator[list[DeepgramTts]]:
    """Constructor-with-teardown: the provider owns a process-long
    `httpx.AsyncClient` (judgment call 3), which a test process should
    close rather than leak a warning per test."""
    built: list[DeepgramTts] = []
    yield built
    for tts in built:
        await tts.aclose()


def _build(built: list[DeepgramTts], settings: Settings) -> DeepgramTts:
    tts = DeepgramTts(settings)
    built.append(tts)
    return tts


async def _collect(tts: DeepgramTts, text: str, *, sentence_no: int = 0) -> list[TtsChunk]:
    chunks: list[TtsChunk] = []
    async with asyncio.timeout(_TIMEOUT_S):
        async for chunk in tts.synthesize(text, sentence_no=sentence_no):
            chunks.append(chunk)
    return chunks


@pytest.fixture
def tts_span_exporter(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """Module-seam tracer monkeypatch — same reasoning as
    test_elevenlabs.py's identically-named fixture (order-independent vs.
    the session-wide provider, synchronous export, no flush)."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(
        deepgram_tts_module, "_tracer", provider.get_tracer("test-deepgram-tts")
    )
    return exporter


def _finished_tts_spans(exporter: InMemorySpanExporter):
    return [s for s in exporter.get_finished_spans() if s.name == "tts.first_byte"]


def test_missing_api_key_fails_fast(settings_factory: SettingsFactory) -> None:
    """Construction-time failure, not first-sentence-of-a-live-call — the
    posture run.py's lazy-deps judgment call 1 depends on."""
    with pytest.raises(ValueError, match="DEEPGRAM_API_KEY"):
        DeepgramTts(settings_factory(deepgram_api_key=None))


async def test_posts_model_params_token_auth_and_text_body(
    settings_factory: SettingsFactory,
    respx_mock: respx.MockRouter,
    make_tts: list[DeepgramTts],
) -> None:
    """The live-validated request shape: POST to the speak URL with the
    model + frozen linear16/24000/none params, `Token` (not Bearer) auth,
    and the sentence as the one JSON body field."""
    route = respx_mock.post(url__startswith=_SPEAK_URL).mock(
        return_value=httpx.Response(200, content=b"\x01\x02")
    )
    tts = _build(make_tts, _settings(settings_factory, deepgram_tts_model="aura-2-thalia-en"))

    await _collect(tts, "Hello Rajesh.", sentence_no=1)

    request = route.calls.last.request
    assert request.url.params["model"] == "aura-2-thalia-en"
    assert request.url.params["encoding"] == "linear16"
    assert request.url.params["sample_rate"] == "24000"
    assert request.url.params["container"] == "none"
    assert request.headers["authorization"] == f"Token {_API_KEY}"
    assert orjson.loads(request.content) == {"text": "Hello Rajesh."}


async def test_streams_chunks_with_empty_alignment_and_sentence_no(
    settings_factory: SettingsFactory,
    respx_mock: respx.MockRouter,
    make_tts: list[DeepgramTts],
) -> None:
    """Each HTTP chunk becomes one `TtsChunk`; Aura provides no character
    timing, so alignment is the contract's documented empty case
    (judgment call 2) — truncation.py's proportional path owns barge-in."""

    async def _chunked() -> AsyncIterator[bytes]:
        yield b"\x01\x02\x03\x04"
        yield b"\x05\x06"

    respx_mock.post(url__startswith=_SPEAK_URL).mock(
        return_value=httpx.Response(200, content=_chunked())
    )
    tts = _build(make_tts, _settings(settings_factory))

    chunks = await _collect(tts, "Two chunks.", sentence_no=7)

    assert [c.pcm for c in chunks] == [b"\x01\x02\x03\x04", b"\x05\x06"]
    assert all(c.alignment == () for c in chunks)
    assert all(c.sentence_no == 7 for c in chunks)


async def test_non_2xx_raises_with_detail_and_no_key_leak(
    settings_factory: SettingsFactory,
    respx_mock: respx.MockRouter,
    make_tts: list[DeepgramTts],
) -> None:
    respx_mock.post(url__startswith=_SPEAK_URL).mock(
        return_value=httpx.Response(
            403, content=b'{"category":"FORBIDDEN","message":"invalid credentials"}'
        )
    )
    tts = _build(make_tts, _settings(settings_factory))

    with pytest.raises(DeepgramTtsError) as excinfo:
        await _collect(tts, "Denied.")

    message = str(excinfo.value)
    assert "403" in message
    assert "invalid credentials" in message
    assert _API_KEY not in message


async def test_tts_first_byte_span_records_ttfb_and_sentence_no(
    settings_factory: SettingsFactory,
    respx_mock: respx.MockRouter,
    make_tts: list[DeepgramTts],
    tts_span_exporter: InMemorySpanExporter,
) -> None:
    """docs/04 §7.2: `tts.first_byte` is this layer's span — one per
    sentence, ended at the first audio byte (judgment call 5)."""
    respx_mock.post(url__startswith=_SPEAK_URL).mock(
        return_value=httpx.Response(200, content=b"\x01\x02")
    )
    tts = _build(make_tts, _settings(settings_factory))

    await _collect(tts, "Timed.", sentence_no=3)

    spans = _finished_tts_spans(tts_span_exporter)
    assert len(spans) == 1
    assert spans[0].attributes is not None
    assert spans[0].attributes["sentence_no"] == 3
    assert spans[0].attributes["tts_ttfb_ms"] >= 0
    assert spans[0].status.status_code != StatusCode.ERROR


async def test_span_ends_with_error_when_stream_fails_before_audio(
    settings_factory: SettingsFactory,
    respx_mock: respx.MockRouter,
    make_tts: list[DeepgramTts],
    tts_span_exporter: InMemorySpanExporter,
) -> None:
    """A failure before the first byte still ends the span (ERROR) — an
    interrupted call must never leak a never-ended span."""
    respx_mock.post(url__startswith=_SPEAK_URL).mock(side_effect=httpx.ConnectError)
    tts = _build(make_tts, _settings(settings_factory))

    with pytest.raises(httpx.ConnectError):
        await _collect(tts, "Never arrives.")

    spans = _finished_tts_spans(tts_span_exporter)
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.ERROR


async def test_zero_audio_completion_ends_span_without_error(
    settings_factory: SettingsFactory,
    respx_mock: respx.MockRouter,
    make_tts: list[DeepgramTts],
    tts_span_exporter: InMemorySpanExporter,
) -> None:
    """A clean 200 with no body is a zero-audio completion, not a failure
    (judgment call 5's elevenlabs-matching posture) — the §7.3 error-rate
    panel must not see a false ERROR."""
    respx_mock.post(url__startswith=_SPEAK_URL).mock(
        return_value=httpx.Response(200, content=b"")
    )
    tts = _build(make_tts, _settings(settings_factory))

    chunks = await _collect(tts, "Silent.")

    assert chunks == []
    spans = _finished_tts_spans(tts_span_exporter)
    assert len(spans) == 1
    assert spans[0].status.status_code != StatusCode.ERROR
