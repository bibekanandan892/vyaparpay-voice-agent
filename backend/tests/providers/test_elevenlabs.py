"""Wire-level tests for `ElevenLabsTts.synthesize()` against an in-process
fake stream-input WS server (tests/support/ws_server.py) — NO live network,
per docs/04 §9.4's provider-mocking rule. Live keys don't exist in CI, so
every wire behavior asserted here is pinned from ElevenLabs' streaming
docs and marked `PINNED(vendor)` so a future live-smoke harness knows
exactly what to re-validate against the real endpoint:

- PINNED(vendor): the `/v1/text-to-speech/{voice_id}/stream-input` path
  with `model_id` + `output_format` query params, and the `xi-api-key`
  handshake header.
- PINNED(vendor): the client message sequence — BOS `{"text": " "}` (text
  frames always end with a single space), the sentence with
  `"flush": true`, then EOS `{"text": ""}`.
- PINNED(vendor): server messages carry base64 `audio`, an `alignment`
  object (`chars` / `charStartTimesMs` / `charDurationsMs`) against the
  INPUT text plus a `normalizedAlignment` variant, and terminate with
  `isFinal: true`. The chunk-relative reading of `charStartTimesMs` is
  app/providers/elevenlabs.py judgment call 4 — explicitly on the
  needs-live-validation list.

SKIP GUARD: skips only when the [voice] extra (websockets) is absent —
CI's gates job installs `.[dev,voice]`, so these run on every CI pass
(same convention as tests/voice/test_peer_session.py).
"""

from __future__ import annotations

import pytest

pytest.importorskip("websockets", reason="[voice] extra not installed")

import asyncio  # noqa: E402
import base64  # noqa: E402
from urllib.parse import parse_qs, urlparse  # noqa: E402

import orjson  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode  # noqa: E402
from websockets.asyncio.server import ServerConnection  # noqa: E402
from websockets.exceptions import ConnectionClosedError, WebSocketException  # noqa: E402

import app.providers.elevenlabs as elevenlabs_module  # noqa: E402
from app.config import Settings  # noqa: E402
from app.domain.voice import TtsChunk  # noqa: E402
from app.providers.elevenlabs import ElevenLabsError, ElevenLabsTts  # noqa: E402
from tests.conftest import SettingsFactory  # noqa: E402
from tests.support.ws_server import ws_server  # noqa: E402

_API_KEY = "test-el-key"
_PRIMARY_VOICE = "voice-primary"
_FALLBACK_VOICE = "voice-backup"
_TIMEOUT_S = 10.0


def _settings(settings_factory: SettingsFactory, url: str, **overrides: object) -> Settings:
    return settings_factory(
        elevenlabs_api_key=_API_KEY,
        elevenlabs_voice_id=_PRIMARY_VOICE,
        elevenlabs_url=url,
        **overrides,
    )


def _audio_msg(
    pcm: bytes,
    *,
    chars: list[str] | None = None,
    starts: list[int] | None = None,
) -> str:
    """One server audio message in the vendor's documented shape. When an
    alignment is scripted, a decoy `normalizedAlignment` rides along with
    obviously-wrong timings, so the provider proves it reads `alignment`
    (input-text timing — what §6.3 truncation needs), never the
    normalized variant."""
    payload: dict = {
        "audio": base64.b64encode(pcm).decode() if pcm else None,
        "isFinal": None,
    }
    if chars is not None:
        assert starts is not None
        payload["alignment"] = {
            "chars": chars,
            "charStartTimesMs": starts,
            "charDurationsMs": [1] * len(chars),
        }
        payload["normalizedAlignment"] = {
            "chars": ["X"] * len(chars),
            "charStartTimesMs": [99_999] * len(chars),
            "charDurationsMs": [1] * len(chars),
        }
    return orjson.dumps(payload).decode()


def _replay_handler(record: dict, script: list[str], *, send_final: bool = True):
    """Reads the three client frames (BOS, sentence, EOS), records them,
    replays `script`, optionally terminates with `isFinal`, closes."""

    async def handler(ws: ServerConnection) -> None:
        record.setdefault("connections", []).append(ws.request.path)
        record["xi_api_key"] = ws.request.headers.get("xi-api-key")
        record["frames"] = [await ws.recv() for _ in range(3)]
        for message in script:
            await ws.send(message)
        if send_final:
            await ws.send('{"isFinal": true}')
        await ws.close()

    return handler


async def _collect(tts: ElevenLabsTts, text: str, *, sentence_no: int = 0) -> list[TtsChunk]:
    chunks: list[TtsChunk] = []
    async with asyncio.timeout(_TIMEOUT_S):
        async for chunk in tts.synthesize(text, sentence_no=sentence_no):
            chunks.append(chunk)
    return chunks


@pytest.fixture
def tts_span_exporter(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """A test-local tracer wired straight into the module's `_tracer` seam.

    Deliberately NOT conftest's shared `span_exporter`: that fixture relies
    on module-level tracers binding to the session-wide provider FIRST, but
    in full-suite order tests/obs/test_tracing.py has already swapped the
    global provider by the time this module's tracer first opens a span —
    the ProxyTracer then caches the wrong provider forever, and the shared
    fixture's `force_flush()` breaks outright once the global has been
    reset to a bare ProxyTracerProvider (observed: setup AttributeError in
    the full suite, green in isolation). A module-seam monkeypatch is
    order-independent, touches no global tracer state, and
    SimpleSpanProcessor exports synchronously so no flush is needed."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(elevenlabs_module, "_tracer", provider.get_tracer("test-elevenlabs-tts"))
    return exporter


def _finished_tts_spans(exporter: InMemorySpanExporter):
    return [s for s in exporter.get_finished_spans() if s.name == "tts.first_byte"]


async def test_connect_path_params_and_api_key_header(
    settings_factory: SettingsFactory,
) -> None:
    """PINNED(vendor): stream-input path + model_id/output_format query,
    key in the `xi-api-key` handshake header only — never in the URL."""
    record: dict = {}
    async with ws_server(_replay_handler(record, [_audio_msg(b"\x00" * 48)])) as url:
        tts = ElevenLabsTts(_settings(settings_factory, url))
        await _collect(tts, "Namaste.")

    parsed = urlparse(record["connections"][0])
    assert parsed.path == f"/v1/text-to-speech/{_PRIMARY_VOICE}/stream-input"
    assert parse_qs(parsed.query) == {
        "model_id": ["eleven_flash_v2_5"],
        "output_format": ["pcm_24000"],
    }
    assert record["xi_api_key"] == _API_KEY
    assert _API_KEY not in record["connections"][0]


async def test_sends_bos_sentence_with_flush_then_eos(
    settings_factory: SettingsFactory,
) -> None:
    """PINNED(vendor): the three-frame client sequence, including the
    protocol's trailing-single-space rule on text frames."""
    record: dict = {}
    async with ws_server(_replay_handler(record, [_audio_msg(b"\x00" * 48)])) as url:
        tts = ElevenLabsTts(_settings(settings_factory, url))
        await _collect(tts, "Your daily limit is twenty-five thousand rupees.")

    assert [orjson.loads(f) for f in record["frames"]] == [
        {"text": " "},
        {"text": "Your daily limit is twenty-five thousand rupees. ", "flush": True},
        {"text": ""},
    ]


async def test_chunks_stream_with_alignment_rebased_across_frames(
    settings_factory: SettingsFactory,
) -> None:
    """Judgment call 4: char offsets accumulate across alignment frames,
    and chunk-relative charStartTimesMs are rebased to sentence-relative
    ms by the exact PCM already yielded (96 bytes = 2 ms at 24 kHz mono
    s16). The decoy normalizedAlignment (99999 ms) must NOT be used."""
    record: dict = {}
    pcm_1, pcm_2 = b"\x01\x00" * 48, b"\x02\x00" * 24  # 96 bytes = 2 ms; 48 bytes = 1 ms
    script = [
        _audio_msg(pcm_1, chars=["H", "i"], starts=[0, 40]),
        _audio_msg(pcm_2, chars=["!"], starts=[5]),
    ]
    async with ws_server(_replay_handler(record, script)) as url:
        tts = ElevenLabsTts(_settings(settings_factory, url))
        chunks = await _collect(tts, "Hi!", sentence_no=2)

    assert chunks == [
        TtsChunk(pcm=pcm_1, alignment=((0, 0), (1, 40)), sentence_no=2),
        TtsChunk(pcm=pcm_2, alignment=((2, 7),), sentence_no=2),  # char 2, 2 ms base + 5
    ]


async def test_pure_audio_and_pure_alignment_frames(
    settings_factory: SettingsFactory,
) -> None:
    """The contract's documented shapes: audio-with-no-alignment yields
    `alignment=()`; an alignment-only frame yields `pcm=b""`; a frame with
    neither (keep-alive/unknown) yields nothing at all."""
    record: dict = {}
    pcm = b"\x03\x00" * 24
    script = [
        _audio_msg(pcm),  # pure audio
        orjson.dumps({"someFutureField": True}).decode(),  # neither -> skipped
        _audio_msg(b"", chars=["A"], starts=[2]),  # pure alignment
    ]
    async with ws_server(_replay_handler(record, script)) as url:
        tts = ElevenLabsTts(_settings(settings_factory, url))
        chunks = await _collect(tts, "A.")

    assert chunks == [
        TtsChunk(pcm=pcm, alignment=(), sentence_no=0),
        # 48 bytes yielded = 1 ms base, + chunk-relative start 2 ms = 3 ms;
        # char_base stays 0 because the pure-audio chunk carried no chars.
        TtsChunk(pcm=b"", alignment=((0, 3),), sentence_no=0),
    ]


async def test_is_final_terminates_the_stream_cleanly(
    settings_factory: SettingsFactory,
) -> None:
    """PINNED(vendor): `isFinal: true` is the end-of-synthesis marker; the
    per-sentence stream ends there even if the socket lingers."""
    record: dict = {}

    async def handler(ws: ServerConnection) -> None:
        record["frames"] = [await ws.recv() for _ in range(3)]
        await ws.send(_audio_msg(b"\x00" * 48))
        await ws.send('{"isFinal": true}')
        # Never sends a close of its own; the client must end at isFinal.
        # wait_closed() (not an unbounded wait) lets serve() join this task
        # once the client's per-sentence teardown closes the socket.
        await ws.wait_closed()

    async with ws_server(handler) as url:
        tts = ElevenLabsTts(_settings(settings_factory, url))
        chunks = await _collect(tts, "Done.")

    assert len(chunks) == 1


async def test_tts_first_byte_span_records_ttfb_and_sentence_no(
    settings_factory: SettingsFactory,
    tts_span_exporter: InMemorySpanExporter,
) -> None:
    """docs/04 §7.2: `tts.first_byte` is THIS layer's span — one per
    synthesize call, ended at the first audio byte, carrying exactly the
    frozen attribute pair."""
    record: dict = {}
    async with ws_server(_replay_handler(record, [_audio_msg(b"\x00" * 48)])) as url:
        tts = ElevenLabsTts(_settings(settings_factory, url))
        await _collect(tts, "Namaste.", sentence_no=3)

    spans = _finished_tts_spans(tts_span_exporter)
    assert len(spans) == 1
    assert spans[0].attributes is not None
    assert spans[0].attributes["sentence_no"] == 3
    assert spans[0].attributes["tts_ttfb_ms"] >= 0


async def test_span_ends_with_error_when_no_audio_ever_arrives(
    settings_factory: SettingsFactory,
    tts_span_exporter: InMemorySpanExporter,
) -> None:
    """A stream that dies before the first byte still ends its span —
    with ERROR status, so a dead TTS shows in the trace, not as a
    leaked never-ended span."""

    async def handler(ws: ServerConnection) -> None:
        await ws.close(code=1011, reason="synthesis backend down")

    async with ws_server(handler) as url:
        tts = ElevenLabsTts(_settings(settings_factory, url))
        with pytest.raises(WebSocketException):
            await _collect(tts, "Namaste.")

    spans = _finished_tts_spans(tts_span_exporter)
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.ERROR


async def test_server_error_frame_raises_elevenlabs_error_without_key_leak(
    settings_factory: SettingsFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """PINNED(vendor): the server can report failure in-band as a JSON
    error frame rather than a close code — surfaced loudly as
    ElevenLabsError; no fallback configured, so it propagates."""
    record: dict = {}
    script = [orjson.dumps({"error": "quota_exceeded", "message": "character limit hit"}).decode()]
    async with ws_server(_replay_handler(record, script, send_final=False)) as url:
        tts = ElevenLabsTts(_settings(settings_factory, url))
        with pytest.raises(ElevenLabsError, match="quota_exceeded") as excinfo:
            await _collect(tts, "Namaste.")

    captured = capsys.readouterr()
    assert _API_KEY not in captured.out
    assert _API_KEY not in captured.err
    assert _API_KEY not in str(excinfo.value)


async def test_malformed_json_raises_loudly(settings_factory: SettingsFactory) -> None:
    record: dict = {}
    async with ws_server(_replay_handler(record, ["garbage {{{", ], send_final=False)) as url:
        tts = ElevenLabsTts(_settings(settings_factory, url))
        with pytest.raises(orjson.JSONDecodeError):
            await _collect(tts, "Namaste.")


async def test_malformed_alignment_shape_raises_loudly(
    settings_factory: SettingsFactory,
) -> None:
    record: dict = {}
    script = [
        orjson.dumps(
            {"audio": base64.b64encode(b"\x00" * 48).decode(), "alignment": {"wrong": []}}
        ).decode()
    ]
    async with ws_server(_replay_handler(record, script, send_final=False)) as url:
        tts = ElevenLabsTts(_settings(settings_factory, url))
        with pytest.raises(KeyError):
            await _collect(tts, "Namaste.")


async def test_retries_once_on_fallback_voice_before_first_byte(
    settings_factory: SettingsFactory,
) -> None:
    """docs/06 §9 via the TtsProvider seam (judgment call 6): primary voice
    fails before any audio -> the same sentence is retried once on the
    configured fallback voice, invisibly to the caller (one chunk
    stream either way)."""
    record: dict = {}
    pcm = b"\x04\x00" * 24

    async def handler(ws: ServerConnection) -> None:
        record.setdefault("connections", []).append(ws.request.path)
        if _PRIMARY_VOICE in ws.request.path:
            await ws.close(code=1011, reason="voice unavailable")
            return
        _ = [await ws.recv() for _ in range(3)]
        await ws.send(_audio_msg(pcm))
        await ws.send('{"isFinal": true}')
        await ws.close()

    async with ws_server(handler) as url:
        tts = ElevenLabsTts(
            _settings(settings_factory, url, elevenlabs_fallback_voice_id=_FALLBACK_VOICE)
        )
        chunks = await _collect(tts, "Namaste.", sentence_no=1)

    assert chunks == [TtsChunk(pcm=pcm, alignment=(), sentence_no=1)]
    assert len(record["connections"]) == 2
    assert _PRIMARY_VOICE in record["connections"][0]
    assert _FALLBACK_VOICE in record["connections"][1]


async def test_no_retry_after_first_audio_byte_yielded(
    settings_factory: SettingsFactory,
) -> None:
    """Judgment call 6's other half: once audio has been yielded, a
    re-synthesis would double-speak the played prefix, so a mid-stream
    failure propagates even WITH a fallback configured — the worker's §9
    degradation ladder owns that case."""
    record: dict = {}

    async def handler(ws: ServerConnection) -> None:
        record.setdefault("connections", []).append(ws.request.path)
        _ = [await ws.recv() for _ in range(3)]
        await ws.send(_audio_msg(b"\x05\x00" * 24))
        await ws.close(code=1011, reason="mid-stream failure")

    async with ws_server(handler) as url:
        tts = ElevenLabsTts(
            _settings(settings_factory, url, elevenlabs_fallback_voice_id=_FALLBACK_VOICE)
        )
        with pytest.raises(ConnectionClosedError):
            await _collect(tts, "Namaste.")

    assert len(record["connections"]) == 1  # fallback was NOT attempted


async def test_missing_api_key_and_voice_id_fail_fast(
    settings_factory: SettingsFactory,
) -> None:
    """Construction refuses immediately, naming the env var, echoing no
    secret material (there is none to echo)."""
    with pytest.raises(ValueError, match="ELEVENLABS_API_KEY"):
        ElevenLabsTts(settings_factory())  # key defaults to None
    with pytest.raises(ValueError, match="ELEVENLABS_VOICE_ID"):
        ElevenLabsTts(settings_factory(elevenlabs_api_key=_API_KEY))  # voice id defaults ""
