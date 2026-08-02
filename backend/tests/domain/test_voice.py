"""Unit tests for app.domain.voice — the frozen Phase-3 voice contract.

Three things this suite pins, because downstream Phase-3 tasks are being
built against them in parallel:

1. Wire fidelity: everything that crosses the wire (SignalMessage,
   DataChannelMessage payloads, IceServer, SessionCredentials) serializes
   to exactly the docs/13 §2.1/§4/§6 example shapes — field names, enum
   string values, and the STUN-entry-omits-credentials rule.
2. Value-object discipline: every model is frozen, and the SttEvent union
   is discriminated (an "is_final partial" is unrepresentable).
3. Protocol structural conformance: a minimal fake per Protocol
   type-checks via an annotated assignment (the same pattern
   tests/agent/test_session_manager.py uses for SessionManagerProto)
   and actually runs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime

import pytest
from pydantic import ValidationError

from app.domain.voice import (
    DC_TYPE_AGENT_STATE,
    DC_TYPE_TRANSCRIPT_FINAL,
    DC_TYPE_TRANSCRIPT_PARTIAL,
    WIRE_ENVELOPE_V,
    AgentState,
    AgentStateMsg,
    AudioFrame,
    DataChannelMessage,
    IceServer,
    ReplySink,
    Sentence,
    SessionCredentials,
    SignalMessage,
    SttEvent,
    SttFinal,
    SttPartial,
    SttProvider,
    TranscriptFinal,
    TranscriptPartial,
    TtsChunk,
    TtsProvider,
    VadModel,
)

# docs/06 §3.2: the STT ingest format is 16 kHz linear16 mono.
STT_SAMPLE_RATE_HZ = 16_000
# One 30 ms Silero hop at 16 kHz (docs/06 §5) = 480 samples of int16.
SILERO_FRAME_SAMPLES = 480


# --------------------------------------------------------------------------
# Wire round-trips (docs/13 §2.1 / §4 / §6 example shapes)
# --------------------------------------------------------------------------


def test_session_credentials_serializes_to_the_docs_13_2_1_wire_shape() -> None:
    """The §2.1 `data` object, byte-for-byte field names — including the
    STUN entry omitting `username`/`credential` under exclude_none."""
    creds = SessionCredentials(
        session_id="a1f3c9",
        signaling_url="wss://voice.vyapar.local/v1/signal",
        signaling_token="st_hV4nQ9pXzL2mR8cT0kWyB6uJ",
        ice_servers=(
            IceServer(urls=("stun:turn.vyapar.local:3478",)),
            IceServer(
                urls=(
                    "turn:turn.vyapar.local:3478?transport=udp",
                    "turns:turn.vyapar.local:5349",
                ),
                username="1784537062:a1f3c9",
                credential="kWx0mB4vQ2nT8hZJc6yUq1RfLpE=",
            ),
        ),
        expires=datetime.fromisoformat("2026-07-24T14:19:22+05:30"),
    )

    assert creds.model_dump(mode="json", exclude_none=True) == {
        "session_id": "a1f3c9",
        "signaling_url": "wss://voice.vyapar.local/v1/signal",
        "signaling_token": "st_hV4nQ9pXzL2mR8cT0kWyB6uJ",
        "ice_servers": [
            {"urls": ["stun:turn.vyapar.local:3478"]},
            {
                "urls": [
                    "turn:turn.vyapar.local:3478?transport=udp",
                    "turns:turn.vyapar.local:5349",
                ],
                "username": "1784537062:a1f3c9",
                "credential": "kWx0mB4vQ2nT8hZJc6yUq1RfLpE=",
            },
        ],
        "expires": "2026-07-24T14:19:22+05:30",
    }


def test_session_credentials_round_trips_through_json() -> None:
    creds = SessionCredentials(
        session_id="a1f3c9",
        signaling_url="wss://voice.vyapar.local/v1/signal",
        signaling_token="st_hV4nQ9pXzL2mR8cT0kWyB6uJ",
        ice_servers=(IceServer(urls=("stun:turn.vyapar.local:3478",)),),
        expires=datetime.fromisoformat("2026-07-24T14:19:22+05:30"),
    )

    assert SessionCredentials.model_validate_json(creds.model_dump_json()) == creds


def test_signal_message_parses_and_re_emits_the_docs_13_6_envelope() -> None:
    wire = {"v": 1, "type": "bye", "payload": {"reason": "user_hangup"}}

    msg = SignalMessage.model_validate(wire)

    assert msg.v == WIRE_ENVELOPE_V
    assert msg.type == "bye"
    assert msg.payload == {"reason": "user_hangup"}
    assert msg.model_dump(mode="json") == wire


def test_signal_message_rejects_a_type_outside_the_docs_13_6_vocabulary() -> None:
    """§6's envelope fixes the signaling vocabulary in the type itself —
    unlike the data channel, an unknown signal type is a protocol error,
    not something to ignore."""
    with pytest.raises(ValidationError):
        SignalMessage(type="renegotiate", payload={})  # type: ignore[arg-type]


def test_data_channel_envelope_round_trips_the_docs_13_4_transcript_final() -> None:
    payload = TranscriptFinal(turn=1, role="agent", text="Hi Rajesh, I can see your payment.")
    envelope = DataChannelMessage(
        type=DC_TYPE_TRANSCRIPT_FINAL,
        seq=9,
        ts=1_784_536_468_200,
        payload=payload.model_dump(),
    )

    wire = envelope.model_dump(mode="json")
    assert wire == {
        "v": 1,
        "type": "transcript.final",
        "seq": 9,
        "ts": 1_784_536_468_200,
        "payload": {"turn": 1, "role": "agent", "text": "Hi Rajesh, I can see your payment."},
    }

    reparsed = DataChannelMessage.model_validate_json(envelope.model_dump_json())
    assert reparsed == envelope
    assert TranscriptFinal.model_validate(reparsed.payload) == payload


def test_data_channel_envelope_accepts_unknown_types_per_the_additive_rule() -> None:
    """docs/13 §9: new message types are additive within /v1, so the
    envelope must be able to represent a type this contract has never
    heard of (the reader ignores it; the parser must not explode)."""
    msg = DataChannelMessage(type="ctx.snapshot", seq=45, ts=1_784_536_455_000, payload={})

    assert msg.type == "ctx.snapshot"


def test_transcript_partial_and_agent_state_payloads_match_the_docs_13_4_examples() -> None:
    partial = TranscriptPartial(turn=2, role="user", text="haan, the two forty five one to")
    state = AgentStateMsg(state=AgentState.LISTENING, turn=1)

    assert partial.model_dump(mode="json") == {
        "turn": 2,
        "role": "user",
        "text": "haan, the two forty five one to",
    }
    assert state.model_dump(mode="json") == {"state": "listening", "turn": 1}


def test_transcript_payload_role_is_the_coarse_user_agent_vocabulary() -> None:
    """docs/12 §4.2's who-spoke vocabulary — 'assistant' is the chat-array
    Role enum's word and must not leak onto the data channel."""
    with pytest.raises(ValidationError):
        TranscriptPartial(turn=1, role="assistant", text="hi")  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Value-object discipline: enums, frozen-ness, discriminated STT union
# --------------------------------------------------------------------------


def test_agent_state_values_are_the_docs_13_4_wire_strings() -> None:
    assert [state.value for state in AgentState] == ["listening", "thinking", "speaking"]


def test_data_channel_type_constants_match_the_docs_13_4_table() -> None:
    assert DC_TYPE_TRANSCRIPT_PARTIAL == "transcript.partial"
    assert DC_TYPE_TRANSCRIPT_FINAL == "transcript.final"
    assert DC_TYPE_AGENT_STATE == "agent.state"


def test_stt_events_are_discriminated_on_is_final() -> None:
    partial = SttPartial(text="I want to", ts_ms=1_240)
    final = SttFinal(text="I want to pay a vendor.", ts_ms=2_105)

    assert partial.is_final is False
    assert final.is_final is True
    # An "is_final partial" is unrepresentable, not a runtime convention.
    with pytest.raises(ValidationError):
        SttPartial(text="oops", ts_ms=0, is_final=True)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        SttFinal(text="oops", ts_ms=0, is_final=False)  # type: ignore[arg-type]


def test_voice_value_objects_are_frozen() -> None:
    """Same freeze discipline as types.py — mutation raises, derived
    copies go through model_copy."""
    frame = AudioFrame(
        pcm16=b"\x00" * (SILERO_FRAME_SAMPLES * 2),
        sample_rate=STT_SAMPLE_RATE_HZ,
        samples=SILERO_FRAME_SAMPLES,
        ts_ms=0,
    )
    chunk = TtsChunk(pcm=b"\x00\x00", alignment=((0, 0), (5, 120)), sentence_no=0)
    sentence = Sentence(text="Your daily limit is twenty-five thousand rupees.", sentence_no=0)

    for frozen_obj, field, value in (
        (frame, "ts_ms", 30),
        (chunk, "sentence_no", 1),
        (sentence, "text", "changed"),
    ):
        with pytest.raises(ValidationError):
            setattr(frozen_obj, field, value)

    bumped = frame.model_copy(update={"ts_ms": 30})
    assert bumped.ts_ms == 30
    assert frame.ts_ms == 0


# --------------------------------------------------------------------------
# Protocol structural conformance (annotated-assignment pattern, as in
# tests/agent/test_session_manager.py) — each fake is the minimal shape
# that satisfies the Protocol, then actually runs.
# --------------------------------------------------------------------------


async def _replay_stt(events: Sequence[SttEvent]) -> AsyncIterator[SttEvent]:
    for event in events:
        yield event


async def _replay_tts(chunks: Sequence[TtsChunk]) -> AsyncIterator[TtsChunk]:
    for chunk in chunks:
        yield chunk


async def _audio_source(frames: Sequence[bytes]) -> AsyncIterator[bytes]:
    for frame in frames:
        yield frame


class _ScriptedStt:
    """Minimal SttProvider double: replays a scripted event sequence and
    records the sample_rate each stream() call asked for."""

    def __init__(self, events: Sequence[SttEvent]) -> None:
        self._events = tuple(events)
        self.sample_rates: list[int] = []

    async def stream(
        self, audio: AsyncIterator[bytes], *, sample_rate: int
    ) -> AsyncIterator[SttEvent]:
        self.sample_rates.append(sample_rate)
        return _replay_stt(self._events)


class _ScriptedTts:
    """Minimal TtsProvider double: one scripted chunk stream per sentence."""

    def __init__(self, chunks: Sequence[TtsChunk]) -> None:
        self._chunks = tuple(chunks)
        self.requests: list[tuple[int, str]] = []

    async def synthesize(self, text: str, *, sentence_no: int) -> AsyncIterator[TtsChunk]:
        self.requests.append((sentence_no, text))
        return _replay_tts(self._chunks)


class _ScriptedVad:
    """Minimal VadModel double: pops one scripted probability per frame —
    exactly how endpoint-policy tests will script §5.3 decision rows."""

    def __init__(self, probs: Sequence[float]) -> None:
        self._probs = list(probs)

    def prob(self, frame_30ms: bytes) -> float:
        return self._probs.pop(0)


class _RecordingSink:
    """Minimal ReplySink double: records what the brain streamed out."""

    def __init__(self) -> None:
        self.sentences: list[tuple[int, int, str]] = []
        self.completed: list[tuple[int, str]] = []

    async def on_sentence(self, text: str, *, turn_no: int, sentence_no: int) -> None:
        self.sentences.append((turn_no, sentence_no, text))

    async def on_turn_complete(self, full_text: str, *, turn_no: int) -> None:
        self.completed.append((turn_no, full_text))


async def test_stt_provider_protocol_conformance() -> None:
    scripted = _ScriptedStt(
        [SttPartial(text="I want to", ts_ms=900), SttFinal(text="I want to pay.", ts_ms=1_400)]
    )
    provider: SttProvider = scripted  # structural conformance check

    stream = await provider.stream(_audio_source([b"\x00\x00"]), sample_rate=STT_SAMPLE_RATE_HZ)
    events = [event async for event in stream]

    assert scripted.sample_rates == [STT_SAMPLE_RATE_HZ]
    assert [event.is_final for event in events] == [False, True]
    assert events[-1].text == "I want to pay."


async def test_tts_provider_protocol_conformance() -> None:
    scripted = _ScriptedTts([TtsChunk(pcm=b"\x01\x02", alignment=((0, 0),), sentence_no=0)])
    provider: TtsProvider = scripted  # structural conformance check

    stream = await provider.synthesize("Namaste Rajesh.", sentence_no=0)
    chunks = [chunk async for chunk in stream]

    assert scripted.requests == [(0, "Namaste Rajesh.")]
    assert chunks[0].pcm == b"\x01\x02"
    assert chunks[0].alignment == ((0, 0),)


def test_vad_model_protocol_conformance() -> None:
    scripted = _ScriptedVad([0.9, 0.1])
    vad: VadModel = scripted  # structural conformance check

    silero_frame = b"\x00" * (SILERO_FRAME_SAMPLES * 2)
    assert vad.prob(silero_frame) == 0.9
    assert vad.prob(silero_frame) == 0.1


async def test_reply_sink_protocol_conformance() -> None:
    recording = _RecordingSink()
    sink: ReplySink = recording  # structural conformance check

    await sink.on_sentence("Your limit is twenty-five thousand.", turn_no=3, sentence_no=0)
    await sink.on_sentence("You have used most of it.", turn_no=3, sentence_no=1)
    await sink.on_turn_complete(
        "Your limit is twenty-five thousand. You have used most of it.", turn_no=3
    )

    assert recording.sentences == [
        (3, 0, "Your limit is twenty-five thousand."),
        (3, 1, "You have used most of it."),
    ]
    assert recording.completed == [
        (3, "Your limit is twenty-five thousand. You have used most of it.")
    ]
