"""Wire-level tests for `DeepgramStt.stream()` against an in-process fake
Deepgram WS server (tests/support/ws_server.py) — NO live network, per
docs/04 §9.4's provider-mocking rule. Live keys don't exist in CI, so every
wire behavior asserted here is pinned from Deepgram's streaming docs and
marked `PINNED(vendor)` so a future live-smoke harness knows exactly what
to re-validate against the real endpoint:

- PINNED(vendor): /v1/listen query params (`model`, `encoding=linear16`,
  `sample_rate`, `channels`, `interim_results`, `smart_format`,
  `endpointing`) and the `Authorization: Token <key>` handshake header.
- PINNED(vendor): audio goes up as binary frames; control frames
  (`KeepAlive`, `CloseStream`) go up as JSON text frames.
- PINNED(vendor): Results messages carry `channel.alternatives[0].transcript`,
  `start`/`duration` seconds, and the `is_final` / `speech_final` flags —
  `is_final` finalizes a SEGMENT (later interims cover only audio after
  it), `speech_final` ends the UTTERANCE. The mapping decision built on
  that distinction is app/providers/deepgram.py judgment call 1.
- PINNED(vendor): a socket idle >~10 s closes (NET-0001) — KeepAlive is
  the documented antidote (interval shrunk here via the test-only
  constructor knob).

SKIP GUARD: skips only when the [voice] extra (websockets) is absent —
CI's gates job installs `.[dev,voice]`, so these run on every CI pass
(same convention as tests/voice/test_peer_session.py).
"""

from __future__ import annotations

import pytest

pytest.importorskip("websockets", reason="[voice] extra not installed")

import asyncio  # noqa: E402
from collections.abc import AsyncIterator  # noqa: E402
from urllib.parse import parse_qs, urlparse  # noqa: E402

import orjson  # noqa: E402
from websockets.asyncio.server import ServerConnection  # noqa: E402
from websockets.exceptions import ConnectionClosedError  # noqa: E402

from app.config import Settings  # noqa: E402
from app.domain.voice import SttEvent, SttFinal, SttPartial  # noqa: E402
from app.providers.deepgram import DeepgramStt  # noqa: E402
from tests.conftest import SettingsFactory  # noqa: E402
from tests.support.ws_server import ws_server  # noqa: E402

_API_KEY = "test-dg-key"
_SAMPLE_RATE = 16_000
_TIMEOUT_S = 10.0


def _settings(settings_factory: SettingsFactory, url: str) -> Settings:
    return settings_factory(deepgram_api_key=_API_KEY, deepgram_url=f"{url}/v1/listen")


def _results(
    transcript: str,
    *,
    is_final: bool = False,
    speech_final: bool = False,
    start: float = 0.0,
    duration: float = 1.0,
) -> str:
    """One Results message in the vendor's documented shape — extra fields
    (confidence, words, channel_index) included on purpose so the mapper
    proves it tolerates fields it doesn't consume."""
    return orjson.dumps(
        {
            "type": "Results",
            "channel_index": [0, 1],
            "start": start,
            "duration": duration,
            "is_final": is_final,
            "speech_final": speech_final,
            "channel": {
                "alternatives": [{"transcript": transcript, "confidence": 0.98, "words": []}]
            },
        }
    ).decode()


async def _audio(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


async def _audio_then_stall(*chunks: bytes) -> AsyncIterator[bytes]:
    """Yields the given chunks, then stalls forever — for tests where the
    provider (not audio exhaustion) must end the stream."""
    for chunk in chunks:
        yield chunk
    await asyncio.Event().wait()


def _replay_handler(record: dict, script: list[str]):
    """A handler that records the handshake + every client frame, waits for
    the client's CloseStream, then replays `script` and closes cleanly —
    the deterministic happy-path server shape."""

    async def handler(ws: ServerConnection) -> None:
        record["path"] = ws.request.path
        record["authorization"] = ws.request.headers.get("Authorization")
        record["frames"] = []
        async for frame in ws:
            record["frames"].append(frame)
            if isinstance(frame, str) and orjson.loads(frame).get("type") == "CloseStream":
                break
        for message in script:
            await ws.send(message)
        await ws.close()

    return handler


async def _collect(stt: DeepgramStt, audio: AsyncIterator[bytes]) -> list[SttEvent]:
    events: list[SttEvent] = []
    async with asyncio.timeout(_TIMEOUT_S):
        async for event in stt.stream(audio, sample_rate=_SAMPLE_RATE):
            events.append(event)
    return events


async def test_connect_sends_pinned_query_params_and_token_header(
    settings_factory: SettingsFactory,
) -> None:
    """PINNED(vendor): the full /v1/listen query string docs/06 §3.2 + the
    provider's judgment calls 2-3 fix, and header auth — key in the
    handshake header only, never in the URL."""
    record: dict = {}
    async with ws_server(_replay_handler(record, [])) as url:
        stt = DeepgramStt(_settings(settings_factory, url))
        events = await _collect(stt, _audio())

    assert events == []
    parsed = urlparse(record["path"])
    assert parsed.path == "/v1/listen"
    assert parse_qs(parsed.query) == {
        "model": ["nova-3"],
        "encoding": ["linear16"],
        "sample_rate": ["16000"],
        "channels": ["1"],
        "interim_results": ["true"],
        "smart_format": ["true"],
        "endpointing": ["250"],
    }
    assert record["authorization"] == f"Token {_API_KEY}"
    assert _API_KEY not in record["path"]


async def test_stream_sends_pcm_binary_frames_verbatim_then_close_stream(
    settings_factory: SettingsFactory,
) -> None:
    """PINNED(vendor): PCM goes up as binary frames, unmodified and in
    order; input end sends the JSON CloseStream control frame."""
    record: dict = {}
    pcm_1, pcm_2 = b"\x01\x02\x03\x04", b"\x05\x06"
    async with ws_server(_replay_handler(record, [])) as url:
        stt = DeepgramStt(_settings(settings_factory, url))
        await _collect(stt, _audio(pcm_1, pcm_2))

    assert record["frames"][:2] == [pcm_1, pcm_2]
    assert orjson.loads(record["frames"][2]) == {"type": "CloseStream"}


async def test_interim_results_map_to_superseding_partials_and_one_final(
    settings_factory: SettingsFactory,
) -> None:
    """Interim -> SttPartial (each superseding the last), speech_final ->
    SttFinal; ts_ms is round((start + duration) * 1000), the media-clock
    end of the transcribed audio (judgment call 7)."""
    record: dict = {}
    script = [
        _results("namaste", start=0.0, duration=0.5),
        _results("namaste asha", start=0.0, duration=1.0),
        _results("namaste asha ji", is_final=True, speech_final=True, start=0.0, duration=1.5),
    ]
    async with ws_server(_replay_handler(record, script)) as url:
        stt = DeepgramStt(_settings(settings_factory, url))
        events = await _collect(stt, _audio(b"\x00\x00"))

    assert events == [
        SttPartial(text="namaste", ts_ms=500),
        SttPartial(text="namaste asha", ts_ms=1000),
        SttFinal(text="namaste asha ji", ts_ms=1500),
    ]


async def test_is_final_segment_accumulates_without_emitting_final(
    settings_factory: SettingsFactory,
) -> None:
    """THE is_final vs speech_final distinction (judgment call 1, pinned
    from vendor docs): a mid-utterance `is_final` finalizes a SEGMENT —
    later interims cover only audio after it, so the provider must
    accumulate — and only `speech_final` ends the UTTERANCE. An
    `is_final`-only message therefore yields a stabilized SttPartial,
    never an SttFinal, and the eventual SttFinal carries the whole
    accumulated utterance."""
    record: dict = {}
    script = [
        _results("i want to", start=0.0, duration=0.8),
        _results("i want to", is_final=True, start=0.0, duration=1.0),
        _results("pay a vendor", start=1.0, duration=0.6),
        _results("pay a vendor.", is_final=True, speech_final=True, start=1.0, duration=0.9),
    ]
    async with ws_server(_replay_handler(record, script)) as url:
        stt = DeepgramStt(_settings(settings_factory, url))
        events = await _collect(stt, _audio(b"\x00\x00"))

    assert events == [
        SttPartial(text="i want to", ts_ms=800),
        SttPartial(text="i want to", ts_ms=1000),  # stabilized, NOT a final
        SttPartial(text="i want to pay a vendor", ts_ms=1600),
        SttFinal(text="i want to pay a vendor.", ts_ms=1900),
    ]
    assert sum(isinstance(event, SttFinal) for event in events) == 1


async def test_accumulator_resets_between_utterances(
    settings_factory: SettingsFactory,
) -> None:
    """A second utterance after a speech_final must start clean — no bleed
    of the first utterance's segments into the second's events."""
    record: dict = {}
    script = [
        _results("hello.", is_final=True, speech_final=True, start=0.0, duration=1.0),
        _results("how are you", start=1.5, duration=0.5),
        _results("how are you?", is_final=True, speech_final=True, start=1.5, duration=1.0),
    ]
    async with ws_server(_replay_handler(record, script)) as url:
        stt = DeepgramStt(_settings(settings_factory, url))
        events = await _collect(stt, _audio(b"\x00\x00"))

    assert events == [
        SttFinal(text="hello.", ts_ms=1000),
        SttPartial(text="how are you", ts_ms=2000),
        SttFinal(text="how are you?", ts_ms=2500),
    ]


async def test_silence_results_emit_no_events(settings_factory: SettingsFactory) -> None:
    """Empty transcripts (interim, segment-final, and an all-silence
    speech_final) are suppressed — no empty partial captions, no empty
    SttFinal opening an empty turn (judgment call 4)."""
    record: dict = {}
    script = [
        _results(""),
        _results("", is_final=True),
        _results("", is_final=True, speech_final=True),
    ]
    async with ws_server(_replay_handler(record, script)) as url:
        stt = DeepgramStt(_settings(settings_factory, url))
        events = await _collect(stt, _audio(b"\x00\x00"))

    assert events == []


async def test_unknown_and_out_of_order_message_types_are_tolerated(
    settings_factory: SettingsFactory,
) -> None:
    """PINNED(vendor): the stream interleaves Metadata / SpeechStarted /
    UtteranceEnd frames with Results — and future message types must
    degrade to a skip, not a crash."""
    record: dict = {}
    script = [
        orjson.dumps({"type": "SpeechStarted", "timestamp": 0.1}).decode(),
        orjson.dumps({"type": "UtteranceEnd", "last_word_end": 1.4}).decode(),
        _results("ok.", is_final=True, speech_final=True, start=0.0, duration=1.0),
        orjson.dumps({"type": "Metadata", "request_id": "req-1"}).decode(),
        orjson.dumps({"type": "SomeFutureThing", "v": 2}).decode(),
    ]
    async with ws_server(_replay_handler(record, script)) as url:
        stt = DeepgramStt(_settings(settings_factory, url))
        events = await _collect(stt, _audio(b"\x00\x00"))

    assert events == [SttFinal(text="ok.", ts_ms=1000)]


async def test_malformed_json_raises_and_never_leaks_the_key(
    settings_factory: SettingsFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    record: dict = {}
    async with ws_server(_replay_handler(record, ["this is not json {{{"])) as url:
        stt = DeepgramStt(_settings(settings_factory, url))
        with pytest.raises(orjson.JSONDecodeError) as excinfo:
            await _collect(stt, _audio(b"\x00\x00"))

    captured = capsys.readouterr()
    assert _API_KEY not in captured.out
    assert _API_KEY not in captured.err
    assert _API_KEY not in str(excinfo.value)


async def test_results_with_missing_shape_raises_loudly(
    settings_factory: SettingsFactory,
) -> None:
    """A Results frame missing `channel` is corrupt data, not an unknown
    type — fail loud (project convention), don't skip it silently."""
    record: dict = {}
    script = [orjson.dumps({"type": "Results", "start": 0.0, "duration": 1.0}).decode()]
    async with ws_server(_replay_handler(record, script)) as url:
        stt = DeepgramStt(_settings(settings_factory, url))
        with pytest.raises(KeyError):
            await _collect(stt, _audio(b"\x00\x00"))


async def test_server_initiated_abnormal_close_propagates(
    settings_factory: SettingsFactory,
) -> None:
    """A mid-utterance server drop is docs/06 §9's "STT stream drops"
    failure — the WORKER owns the reconnect, so this layer must surface
    ConnectionClosedError loudly rather than retry (judgment call 6)."""

    async def handler(ws: ServerConnection) -> None:
        await ws.recv()  # one audio frame arrives...
        await ws.close(code=1011, reason="deepgram internal error")

    async with ws_server(handler) as url:
        stt = DeepgramStt(_settings(settings_factory, url))
        with pytest.raises(ConnectionClosedError):
            await _collect(stt, _audio_then_stall(b"\x00\x00"))


async def test_keepalive_sent_when_audio_stalls(settings_factory: SettingsFactory) -> None:
    """PINNED(vendor): KeepAlive JSON text frames keep an audio-idle socket
    open. The interval is shrunk via the test-only constructor knob; the
    server answers the KeepAlive with a final so the test can also prove
    the stream stays usable across the stall."""
    got_keepalive = asyncio.Event()

    async def handler(ws: ServerConnection) -> None:
        async for frame in ws:
            if isinstance(frame, str) and orjson.loads(frame).get("type") == "KeepAlive":
                got_keepalive.set()
                await ws.send(_results("still here.", is_final=True, speech_final=True))
                break
        # Hold the socket open until the client aborts — wait_closed() (not an
        # unbounded wait) so the serve() context can join this handler task.
        await ws.wait_closed()

    async with ws_server(handler) as url:
        stt = DeepgramStt(_settings(settings_factory, url), keepalive_interval_s=0.05)
        stream = stt.stream(_audio_then_stall(b"\x00\x00"), sample_rate=_SAMPLE_RATE)
        async with asyncio.timeout(_TIMEOUT_S):
            event = await anext(stream)
        await stream.aclose()

    assert got_keepalive.is_set()
    assert event == SttFinal(text="still here.", ts_ms=1000)


async def test_missing_api_key_fails_fast_with_clear_error(
    settings_factory: SettingsFactory,
) -> None:
    """No key -> construction refuses immediately (fail-fast at wiring
    time, not on a live call's first turn), naming the env var without
    echoing any secret material."""
    with pytest.raises(ValueError, match="DEEPGRAM_API_KEY"):
        DeepgramStt(settings_factory())  # deepgram_api_key defaults to None
