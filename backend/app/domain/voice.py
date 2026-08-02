"""Frozen Phase-3 voice contract — the value types and Protocols every
module in app/voice/ (AudioIngress, VadEndpointer, AudioEgress,
SignalingServer, PeerSession, VoiceAgentWorker) and the voice-facing
providers (DeepgramStt, ElevenLabsTts) pass between each other.

This file extends the Phase-2 freeze discipline (app/domain/types.py,
app/domain/interfaces.py) to the voice pipeline: pinned once, up front,
so the parallel Phase-3 tasks build against a shared contract instead of
each inventing a slightly different SttEvent or TtsChunk shape. If an
implementation needs to deviate from a signature here, that's a contract
change — update this file first (and note why), don't silently drift.

Anything that crosses the wire (SignalMessage, DataChannelMessage and its
payload models, IceServer, SessionCredentials) mirrors docs/13-api-contracts.md
§2.1/§4/§6 field-for-field; the cross-language truth for those shapes is
protocol/ (docs/13 §8), which CI round-trips against these models.

All value objects are frozen pydantic models: never mutate one in place,
build a new instance instead (`model_copy(update={...})` when you need a
derived copy) — see ~/.claude/rules/ecc/common/coding-style.md.

Judgment calls made here, recorded so review argues with the reasoning
and not the diff:

1. `SttPartial.is_final` / `SttFinal.is_final` are `Literal[False]` /
   `Literal[True]` rather than a shared plain bool — the `SttEvent`
   union stays discriminated and an "is_final partial" is
   unrepresentable, instead of a runtime convention.
2. Both STT events carry `ts_ms` (worker media-clock milliseconds since
   stream start) — the spec left "ts fields as appropriate" open, and a
   single monotonic media-clock field is what endpoint/barge-in math
   (docs/06 §5-§6) actually consumes.
3. `TtsChunk.alignment` is a tuple of `(char_offset, ms)` pairs, not a
   list — the frozen-immutability discipline types.py already applies to
   `Message.tool_calls` / `ContextBundle.conversation`.
4. `DataChannelMessage.type` AND `SignalMessage.type` are both open
   `str`, not a closed Literal (this was revised from an earlier draft
   that pinned SignalMessage.type as a Literal): docs/13 §9 says
   verbatim that new *signaling or data-channel* message types are
   allowed without a version bump, and requires clients to "ignore
   unknown envelope types" from day one — §6 only enumerates the
   current v1 vocabulary, it doesn't freeze it. A closed Literal would
   make `SignalMessage.model_validate(...)` raise on exactly the future
   types §9 says must degrade gracefully. Both channels' *currently
   known* types are pinned as SIG_TYPE_*/DC_TYPE_* constants instead so
   no emitter or test hand-types a string that could drift (the same
   rule app/obs/tracing.py applies to span names).
5. Transcript payload `role` is `Literal["user", "agent"]` — docs/12
   §4.2's coarse who-spoke vocabulary, deliberately NOT the chat-array
   `Role` enum (see Role's own docstring in types.py for why the two
   vocabularies must not be conflated).
6. `AgentState` overlaps `TurnState` on three of its four values on
   purpose: TurnState is the internal per-turn state machine (it has
   TRANSCRIBING), AgentState is the wire vocabulary of the `agent.state`
   payload (docs/13 §4 shows exactly listening|thinking|speaking).
   Collapsing TRANSCRIBING into the wire's "thinking" is the emitter's
   job, not a fourth wire state.
7. The async-iterator Protocol methods use the same
   `async def ... -> AsyncIterator[...]` spelling as
   interfaces.py's `LLMProvider.stream()` — including its known mypy
   caveat (async-generator implementations need one documented cast;
   see tests/agent/test_llm_router.py) — because contract-file
   consistency beats a private spelling improvement.
8. `IceServer.credential` and `SessionCredentials.signaling_token` are
   plain `str`, not `SecretStr` — both MUST serialize in cleartext on
   the wire, and SecretStr's default JSON dump masks the value, which
   would be wrong here. Instead both fields use `Field(repr=False)` so
   `repr()`/`str()`/an f-string interpolation can't leak them (the
   thing SecretStr protects against in config.py), while `.to_wire()`
   on both models is the one sanctioned path to their real cleartext
   wire shape — never call bare `.model_dump()` on either.

Docs: docs/06-voice-pipeline.md (thresholds, events, barge-in semantics),
docs/13-api-contracts.md §2/§4/§6 (wire shapes), docs/05-agent-architecture.md
§3.2 (the ConversationManager seam ReplySink feeds).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

_FROZEN = ConfigDict(frozen=True)

# Envelope version shared by the signaling and data-channel protocols
# (docs/13 §4/§6: `"v": 1`). Bumps only with a /v2 breaking change
# (docs/13 §9), never independently per channel.
WIRE_ENVELOPE_V: Final = 1

# The server->client data-channel types Phase 3 emits (docs/13 §4's table).
# Pinned as constants so no emitter or test hand-types a string that could
# drift from the client's switch — the same rule tracing.py applies to span
# names. The client->server `ctx.*` types are protocol/'s (docs/08's) to pin.
DC_TYPE_TRANSCRIPT_PARTIAL: Final = "transcript.partial"
DC_TYPE_TRANSCRIPT_FINAL: Final = "transcript.final"
DC_TYPE_AGENT_STATE: Final = "agent.state"

# The signaling vocabulary docs/13 §6 currently enumerates. type stays an
# open str (judgment call 4) — these are what THIS codebase emits/expects,
# not an exhaustive validation set.
SIG_TYPE_OFFER: Final = "offer"
SIG_TYPE_ANSWER: Final = "answer"
SIG_TYPE_ICE: Final = "ice"
SIG_TYPE_BYE: Final = "bye"
SIG_TYPE_ERROR: Final = "error"
SIG_TYPE_PING: Final = "ping"
SIG_TYPE_PONG: Final = "pong"


# --------------------------------------------------------------------------
# Audio frames (worker-internal — never serialized to a wire)
# --------------------------------------------------------------------------


class AudioFrame(BaseModel):
    """One hop of decoded uplink audio as AudioIngress hands it out —
    16 kHz mono linear16 PCM (docs/06 §3.2), already resampled from the
    48 kHz Opus wire format. `samples` is the frame length in samples
    (a 30 ms Silero hop at 16 kHz is 480); `ts_ms` is the worker
    media-clock position of the frame's start."""

    model_config = _FROZEN

    pcm16: bytes
    sample_rate: int
    samples: int
    ts_ms: int


# --------------------------------------------------------------------------
# STT events (docs/06 §1 — Deepgram partials + finals)
# --------------------------------------------------------------------------


class SttPartial(BaseModel):
    """One interim transcript for the in-flight utterance. Each partial
    supersedes the previous one wholesale (the overlay keys captions on
    (turn, role) and replaces, docs/13 §4) — partials are never
    concatenated. Also the completeness-hold's input (docs/06 §5)."""

    model_config = _FROZEN

    text: str
    is_final: Literal[False] = False
    ts_ms: int


class SttFinal(BaseModel):
    """The authoritative transcript for one utterance, emitted once after
    the endpoint. This is the string that opens the brain's turn
    (`ConversationManager.on_stt_final`, interfaces.py)."""

    model_config = _FROZEN

    text: str
    is_final: Literal[True] = True
    ts_ms: int


SttEvent = SttPartial | SttFinal


# --------------------------------------------------------------------------
# TTS chunks and sentence dispatch (docs/06 §4.2, §6.3)
# --------------------------------------------------------------------------


class Sentence(BaseModel):
    """One chunker-emitted sentence, dispatched to TTS the moment it is
    complete (docs/06 §4.2 — only sentence 0's TTFB is on the critical
    path). `sentence_no` is 0-based within the turn."""

    model_config = _FROZEN

    text: str
    sentence_no: int


class TtsChunk(BaseModel):
    """One streamed synthesis chunk for one sentence. `pcm` is provider
    output PCM (24 kHz mono for ElevenLabs Flash, docs/06 §3.2 —
    AudioEgress owns the upsample to the 48 kHz wire). `alignment` is
    the character-timing pairs `(char_offset, ms)` for this chunk —
    the input barge-in truncation maps played milliseconds back through
    to a character offset (docs/06 §6.3). Empty when the provider sent
    a pure-audio chunk with no alignment frame."""

    model_config = _FROZEN

    pcm: bytes
    alignment: tuple[tuple[int, int], ...] = ()
    sentence_no: int


# --------------------------------------------------------------------------
# Agent state — the wire vocabulary of `agent.state` (docs/13 §4)
# --------------------------------------------------------------------------


class AgentState(StrEnum):
    """What the ConversationOverlay's indicator renders. The overlay
    renders state, never infers it (docs/13 §4) — see judgment call 6
    for why this is not TurnState."""

    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"


# --------------------------------------------------------------------------
# Wire envelopes (docs/13 §4 data channel, §6 signaling)
# --------------------------------------------------------------------------


class SignalMessage(BaseModel):
    """One signaling-WebSocket message (docs/13 §6): control plane only —
    SDP, ICE, teardown, keepalive. No seq/ts: the WS is ordered and the
    protocol is request-shaped, so the envelope carries neither."""

    model_config = _FROZEN

    v: int = WIRE_ENVELOPE_V
    type: str
    payload: dict[str, Any]


class DataChannelMessage(BaseModel):
    """One `ctx` data-channel envelope (docs/13 §4). Each direction runs
    its own `seq` counter; `ts` is epoch milliseconds at send. `payload`
    stays a raw dict at the envelope level — the typed Phase-3 payload
    models below are applied per `type` at the emit/parse boundary, and
    unknown types must stay representable (docs/13 §9's ignore rule)."""

    model_config = _FROZEN

    v: int = WIRE_ENVELOPE_V
    type: str
    seq: int
    ts: int
    payload: dict[str, Any]


class TranscriptPartial(BaseModel):
    """`transcript.partial` payload — live captions. User side: throttled
    STT interims; agent side: per sentence as dispatched to TTS
    (docs/13 §4). Replaces the previous partial for its (turn, role)."""

    model_config = _FROZEN

    turn: int
    role: Literal["user", "agent"]
    text: str


class TranscriptFinal(BaseModel):
    """`transcript.final` payload — the authoritative caption for one
    (turn, role), sent once; freezes the overlay's caption. After a
    barge-in the agent-side text is the truncated played-words text,
    not the full generation (docs/06 §6.3)."""

    model_config = _FROZEN

    turn: int
    role: Literal["user", "agent"]
    text: str


class AgentStateMsg(BaseModel):
    """`agent.state` payload — one indicator transition (docs/13 §4)."""

    model_config = _FROZEN

    state: AgentState
    turn: int


# --------------------------------------------------------------------------
# Session mint (docs/13 §2.1 — the connect bundle POST /v1/sessions returns)
# --------------------------------------------------------------------------


class IceServer(BaseModel):
    """One `ice_servers[]` entry, passed verbatim to PeerConnection
    construction (docs/13 §2.1). STUN entries carry only `urls`; the
    TURN entry adds the HMAC credential pair (docs/13 §7), a live
    bearer secret for its 10-min TTL — `credential` is excluded from
    `repr()`/`str()` (judgment call 8) so an accidental
    `logger.info(f"...{ice_server}")` or exception traceback can't
    print it, mirroring config.py's SecretStr discipline. It stays a
    plain `str`, not SecretStr, because it MUST serialize in cleartext
    on the wire — call `.to_wire()`, not `.model_dump()`, to get the
    exact §2.1 shape (a STUN entry omits `username`/`credential`
    entirely rather than nulling them; relying on callers to remember
    `exclude_none=True` is how that shape silently breaks)."""

    model_config = _FROZEN

    urls: tuple[str, ...]
    username: str | None = None
    credential: str | None = Field(default=None, repr=False)

    def to_wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class SessionCredentials(BaseModel):
    """The `data` object of a `201 POST /v1/sessions` response (docs/13
    §2.1) — everything the client needs to connect. `expires` is the
    signaling token's connect deadline (5-min TTL, docs/13 §6.2), not
    the session's lifetime; it serializes to the ISO-8601 string the
    wire example shows. `signaling_token` is a one-time bearer
    credential verified-and-burned before any SDP is read (docs/13
    §6.2) — excluded from `repr()`/`str()` for the same reason as
    `IceServer.credential` (judgment call 8). Call `.to_wire()` for the
    exact §2.1 response shape (nested `ice_servers` need the same
    exclude_none treatment `IceServer.to_wire()` gives each entry)."""

    model_config = _FROZEN

    session_id: str
    signaling_url: str
    signaling_token: str = Field(repr=False)
    ice_servers: tuple[IceServer, ...]
    expires: datetime

    def to_wire(self) -> dict[str, Any]:
        # ice_servers needs IceServer.to_wire()'s per-entry exclude_none,
        # not the plain model_dump() every other field gets -- so this
        # can't be one bare `self.model_dump(mode="json")` call. If a
        # field is ever added to this model, add it here too.
        return {
            **self.model_dump(mode="json", exclude={"ice_servers"}),
            "ice_servers": [s.to_wire() for s in self.ice_servers],
        }


# --------------------------------------------------------------------------
# Provider Protocols (docs/06 §1.1 — the vendor seams inside the worker)
# --------------------------------------------------------------------------


class SttProvider(Protocol):
    """docs/06 canon §3: DeepgramStt implements this; swapping the STT
    vendor never touches the wire format the peers negotiated. `audio`
    is the raw 16 kHz linear16 PCM byte stream AudioIngress fans out
    (frame boundaries are the provider's concern, not the caller's)."""

    async def stream(
        self, audio: AsyncIterator[bytes], *, sample_rate: int
    ) -> AsyncIterator[SttEvent]: ...


class TtsProvider(Protocol):
    """docs/06 canon §3: ElevenLabsTts implements this — one call per
    chunker-emitted sentence (docs/06 §4.2), yielding audio chunks with
    character-timing alignment as they stream. The §9 mid-sentence
    retry-on-backup-voice policy lives behind this seam too: callers
    see one stream per sentence either way."""

    async def synthesize(self, text: str, *, sentence_no: int) -> AsyncIterator[TtsChunk]: ...


class VadModel(Protocol):
    """The Silero seam (docs/06 §5): one speech probability per 30 ms
    frame. Split from VadEndpointer on purpose so endpoint-policy tests
    script probability sequences against the §5.3 decision table with
    no onnxruntime in the loop."""

    def prob(self, frame_30ms: bytes) -> float: ...


class ReplySink(Protocol):
    """Where the brain's streamed reply lands, sentence by sentence —
    consumed by the sanctioned ConversationManager streaming seam
    (docs/05 §3.2's Phase-3 evolution; defined here first so the
    implementation PR extends a contract instead of inventing one).
    The voice pipeline's implementation dispatches each sentence to
    TtsProvider.synthesize(); `on_turn_complete` carries the full
    finalized reply text for transcript/summary bookkeeping."""

    async def on_sentence(self, text: str, *, turn_no: int, sentence_no: int) -> None: ...

    async def on_turn_complete(self, full_text: str, *, turn_no: int) -> None: ...
