"""VoiceAgentWorker turn-machine tests — the whole T5.1 deliverable against
fakes only (FakeVad/FakeStt/FakeTts/FakeBrain from tests/fakes.py plus a
recording egress here): the docs/06 §4 happy path, §6 barge-in
duck/commit/false-positive, §6.3 truncation, §9's STT-death re-ask and
TTS mid-stream skip, and the docs/04 §7.2 span rows this layer owns.

The egress is a recording double rather than the real `AudioEgress`
because the properties under test are the worker's CALL ORDERING against
it — specifically that a committed barge-in calls `flush()` and then
`resume()` (audio_egress judgment call 9: `flush()` deliberately does not
resume, so a worker that forgets leaves the next reply playing silence).
A real AudioEgress would swallow that ordering into internal state where
no assertion can see it. The real egress has its own tests
(tests/voice/test_audio_egress.py); the two meet for real in
tests/e2e/test_voice_pipeline_e2e.py.

SKIP GUARD: skips only when the [voice] extra (av) is absent — the worker
imports the audio modules, which import av (same convention as
tests/voice/test_peer_session.py).
"""

from __future__ import annotations

import pytest

pytest.importorskip("av", reason="[voice] extra not installed")

import asyncio  # noqa: E402
from collections.abc import Callable, Iterator  # noqa: E402
from typing import Any, cast  # noqa: E402

from opentelemetry import trace as otel_trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)
from opentelemetry.util._once import Once  # noqa: E402

from app.config import Settings  # noqa: E402
from app.domain.voice import (  # noqa: E402
    DC_TYPE_AGENT_STATE,
    DC_TYPE_TRANSCRIPT_FINAL,
    DC_TYPE_TRANSCRIPT_PARTIAL,
    DataChannelMessage,
    SttFinal,
    SttPartial,
    SttProvider,
    TtsChunk,
    TtsProvider,
    VadModel,
)
from app.obs.tracing import (  # noqa: E402
    SPAN_STT_FINAL,
    SPAN_TURN,
    setup_observability,  # noqa: E402
)
from app.voice import worker as worker_module  # noqa: E402
from app.voice.audio_egress import FlushResult  # noqa: E402
from app.voice.audio_ingress import VAD_HOP_BYTES  # noqa: E402
from app.voice.worker import REASK_TEXT, VoiceAgentWorker  # noqa: E402
from tests.fakes import (  # noqa: E402
    FAKE_TTS_MS_PER_CHAR,
    FakeBrain,
    FakeStt,
    FakeTts,
    FakeVad,
    fake_tts_chunk,
)

# One 32 ms hop of (silent) PCM — the VAD probability is scripted, so the
# bytes only have to be the right length.
HOP_PCM = b"\x00" * VAD_HOP_BYTES

# docs/06 §5 defaults (Settings): 200 ms min-speech at 32 ms/hop -> 7
# contiguous speech hops confirm SpeechStart; >= 250 ms trailing silence
# with a complete partial -> Endpoint on the 8th silence hop.
SPEECH_HOPS_TO_CONFIRM = 7
SILENCE_HOPS_TO_ENDPOINT = 8

SPEECH_PROB = 0.9
SILENCE_PROB = 0.05

# TtsChunk.pcm is 24 kHz mono s16 -> 48 bytes per millisecond.
PCM_BYTES_PER_MS = 48


async def _until(predicate: Callable[[], bool], *, message: str, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"timed out waiting for {message}")


class _FakeClock:
    """Monotonic epoch-ms stand-in: every read advances by 1 ms, so the
    envelope `ts` assertions are deterministic AND strictly ordered."""

    def __init__(self, start: int = 1_784_536_440_000) -> None:
        self.now = start

    def __call__(self) -> int:
        self.now += 1
        return self.now


class FakeEgress:
    """Recording `app.voice.speech.EgressLike` — see the module docstring
    for why the barge-in assertions need this rather than the real
    AudioEgress. `auto_play=True` models an egress that plays what it is
    given instantly (so the playout wait resolves); `set_played()` pins a
    partial-playback position for the §6.3 truncation math."""

    def __init__(self, *, auto_play: bool = True) -> None:
        self.calls: list[str] = []
        self.chunks: list[TtsChunk] = []
        self.flushes: list[FlushResult] = []
        self._played_ms = 0
        self._auto_play = auto_play

    # -- enqueue side ------------------------------------------------
    def enqueue(self, chunk: TtsChunk) -> None:
        self.calls.append("enqueue")
        self.chunks.append(chunk)
        if self._auto_play:
            self._played_ms += len(chunk.pcm) // PCM_BYTES_PER_MS

    def drain(self) -> None:
        self.calls.append("drain")

    # -- barge-in primitives (docs/06 §6.1) ---------------------------
    def pause(self) -> None:
        self.calls.append("pause")

    def resume(self) -> None:
        self.calls.append("resume")

    def flush(self) -> FlushResult:
        self.calls.append("flush")
        result = FlushResult(played_ms=self._played_ms, dropped_ms=0, dropped_sentences=())
        self.flushes.append(result)
        return result

    @property
    def played_ms(self) -> int:
        return self._played_ms

    # -- test control -------------------------------------------------
    def set_played(self, ms: int) -> None:
        self._played_ms = ms

    @property
    def barge_in_calls(self) -> list[str]:
        """Only the §6.1 primitives, in order — what the ordering
        assertions are actually about."""
        return [c for c in self.calls if c in ("pause", "resume", "flush")]


class Rig:
    """One worker over one AudioIngress, with every collaborator faked."""

    def __init__(
        self,
        settings: Settings,
        *,
        egress: FakeEgress | None = None,
        stt_final_timeout_s: float = 0.2,
    ) -> None:
        from app.voice.audio_ingress import AudioIngress

        self.ingress = AudioIngress()
        self.egress = egress if egress is not None else FakeEgress()
        self.stt = FakeStt()
        self.tts = FakeTts()
        self.brain = FakeBrain()
        self.vad = FakeVad()
        self.clock = _FakeClock()
        self.messages: list[DataChannelMessage] = []
        self.worker = VoiceAgentWorker(
            "sess-worker",
            ingress=self.ingress,
            egress=self.egress,
            # The fakes are async generators; the frozen Protocols spell
            # `async def ... -> AsyncIterator` (domain judgment call 7's
            # documented mypy wart) — the same cast convention as
            # tests/e2e/test_canonical_conversation.py's FakeLLM.
            stt=cast(SttProvider, self.stt),
            tts=cast(TtsProvider, self.tts),
            vad=cast(VadModel, self.vad),
            brain_factory=self._build_brain,
            send_message=self.messages.append,
            settings=settings,
            now_ms=self.clock,
            stt_final_timeout_s=stt_final_timeout_s,
        )
        self._task: asyncio.Task[None] | None = None

    async def _build_brain(self) -> FakeBrain:
        return self.brain

    async def start(self) -> None:
        self._task = asyncio.create_task(self.worker.run(), name="rig-worker")
        await _until(lambda: self.stt.streams_opened >= 1, message="the first STT stream")

    async def push_hops(self, *probs: float) -> None:
        """Feed one 32 ms VAD hop per scripted probability and wait for
        the worker's VAD loop to consume them all."""
        before = len(self.vad.calls)
        self.vad.script(*probs)
        for _ in probs:
            self.ingress.push_pcm(HOP_PCM, sample_rate=16_000)
        await _until(
            lambda: len(self.vad.calls) >= before + len(probs),
            message=f"{len(probs)} VAD hops consumed",
        )

    async def drive_endpoint(self) -> None:
        """The §5.3 row-3 conjunction: min-speech confirmation, then
        >= 250 ms of trailing silence with a complete partial."""
        await self.push_hops(*[SPEECH_PROB] * SPEECH_HOPS_TO_CONFIRM)
        await self.push_hops(*[SILENCE_PROB] * SILENCE_HOPS_TO_ENDPOINT)

    def messages_of(self, type_: str, *, role: str | None = None) -> list[DataChannelMessage]:
        out = [m for m in self.messages if m.type == type_]
        if role is not None:
            out = [m for m in out if m.payload.get("role") == role]
        return out

    def states(self) -> list[str]:
        return [str(m.payload["state"]) for m in self.messages_of(DC_TYPE_AGENT_STATE)]

    async def stop(self) -> None:
        self.ingress.close()
        if self._task is not None:
            await asyncio.wait_for(self._task, timeout=5)


async def _open_turn(rig: Rig, utterance: str) -> None:
    """Push a caption partial + the authoritative final (the common
    ordering — worker judgment call 7 buffers it), then drive the
    endpoint that opens the turn."""
    rig.stt.push(SttPartial(text=utterance, ts_ms=100))
    await _until(
        lambda: bool(rig.messages_of(DC_TYPE_TRANSCRIPT_PARTIAL, role="user")),
        message="the user caption partial",
    )
    rig.stt.push(SttFinal(text=utterance, ts_ms=200))
    await asyncio.sleep(0)  # let the supervisor route the final
    await rig.drive_endpoint()


# --------------------------------------------------------------------------
# Happy path (docs/06 §4)
# --------------------------------------------------------------------------


async def test_full_turn_happy_path(settings: Settings) -> None:
    """endpoint -> STT final -> brain -> sentences -> egress chunks, with
    the docs/13 §4 envelopes in order and monotonic seq/ts."""
    rig = Rig(settings)
    rig.brain.script("First sentence. Second one.")
    await rig.start()
    try:
        await _open_turn(rig, "What happened to my payment?")
        await _until(
            lambda: bool(rig.messages_of(DC_TYPE_TRANSCRIPT_FINAL, role="agent")),
            message="the agent transcript final",
        )
    finally:
        await rig.stop()

    # The brain saw exactly the STT final's text.
    assert rig.brain.calls == ["What happened to my payment?"]

    # Sentence-level dispatch (§4.2): one synthesize() per sentence, in
    # order, numbered 0-based within the turn.
    assert rig.tts.calls == [("First sentence.", 0), ("Second one.", 1)]

    # Both sentences' audio reached the egress, and the turn drained it
    # (audio_egress judgment call 6) so the last millisecond plays.
    assert len(rig.egress.chunks) == 2
    assert [c.sentence_no for c in rig.egress.chunks] == [0, 1]
    assert rig.egress.calls.count("drain") == 1

    # The full docs/13 §4 envelope sequence for one turn, in order.
    assert [(m.type, m.payload.get("role"), m.payload.get("state")) for m in rig.messages] == [
        (DC_TYPE_TRANSCRIPT_PARTIAL, "user", None),
        (DC_TYPE_AGENT_STATE, None, "thinking"),
        (DC_TYPE_TRANSCRIPT_FINAL, "user", None),
        (DC_TYPE_TRANSCRIPT_PARTIAL, "agent", None),
        (DC_TYPE_AGENT_STATE, None, "speaking"),
        (DC_TYPE_TRANSCRIPT_PARTIAL, "agent", None),
        (DC_TYPE_TRANSCRIPT_FINAL, "agent", None),
        (DC_TYPE_AGENT_STATE, None, "listening"),
    ]

    # seq is 1-based, gapless, per call; ts is stamped monotonically.
    assert [m.seq for m in rig.messages] == list(range(1, len(rig.messages) + 1))
    assert all(m.v == 1 for m in rig.messages)
    timestamps = [m.ts for m in rig.messages]
    assert timestamps == sorted(timestamps)
    assert len(set(timestamps)) == len(timestamps)

    # Agent captions are CUMULATIVE per sentence (judgment call 9): a
    # per-sentence-only payload would erase sentence 0's caption.
    agent_partials = [
        m.payload["text"] for m in rig.messages_of(DC_TYPE_TRANSCRIPT_PARTIAL, role="agent")
    ]
    assert agent_partials == ["First sentence.", "First sentence. Second one."]

    # Every envelope belongs to turn 1.
    assert {m.payload["turn"] for m in rig.messages} == {1}
    final_user = rig.messages_of(DC_TYPE_TRANSCRIPT_FINAL, role="user")[0]
    final_agent = rig.messages_of(DC_TYPE_TRANSCRIPT_FINAL, role="agent")[0]
    assert final_user.payload["text"] == "What happened to my payment?"
    assert final_agent.payload["text"] == "First sentence. Second one."


async def test_two_turns_number_sequentially(settings: Settings) -> None:
    rig = Rig(settings)
    rig.brain.script("Reply one.", "Reply two.")
    await rig.start()
    try:
        await _open_turn(rig, "First question?")
        await _until(
            lambda: len(rig.messages_of(DC_TYPE_TRANSCRIPT_FINAL, role="agent")) == 1,
            message="turn 1 to finish",
        )
        await _open_turn(rig, "Second question?")
        await _until(
            lambda: len(rig.messages_of(DC_TYPE_TRANSCRIPT_FINAL, role="agent")) == 2,
            message="turn 2 to finish",
        )
    finally:
        await rig.stop()

    assert rig.brain.calls == ["First question?", "Second question?"]
    agent_finals = rig.messages_of(DC_TYPE_TRANSCRIPT_FINAL, role="agent")
    assert [m.payload["turn"] for m in agent_finals] == [1, 2]
    assert [m.payload["text"] for m in agent_finals] == ["Reply one.", "Reply two."]


# --------------------------------------------------------------------------
# Barge-in (docs/06 §6)
# --------------------------------------------------------------------------


async def _speak_until_audio(rig: Rig, reply: str, hold: asyncio.Event) -> None:
    """Open a turn whose single sentence keeps its TTS stream in flight
    (the `hold` event), so the worker sits in SPEAKING with a genuinely
    live synthesis task for the barge-in to interrupt."""
    rig.brain.script(reply)
    rig.tts.script(reply, fake_tts_chunk(reply), hold=hold)
    await _open_turn(rig, "Tell me about the limit.")
    await _until(lambda: "speaking" in rig.states(), message="the SPEAKING transition")


async def test_barge_in_ducks_then_commits_with_flush_then_resume(settings: Settings) -> None:
    """§6.1's two phases, and THE sharp edge: `flush()` does not resume,
    so the worker must — this test fails if that `resume()` is dropped."""
    egress = FakeEgress(auto_play=False)
    rig = Rig(settings, egress=egress)
    hold = asyncio.Event()
    reply = "Alpha beta gamma delta."
    await rig.start()
    try:
        await _speak_until_audio(rig, reply, hold)
        # 230 ms of audio enqueued (10 ms/char); pin playback partway so
        # the §6.3 mapping has a real position to resolve.
        assert egress.played_ms == 0
        egress.set_played(155)

        # Phase 1 — the FIRST speech frame during SPEAKING ducks, and
        # nothing is cancelled yet.
        await rig.push_hops(SPEECH_PROB)
        assert egress.barge_in_calls == ["pause"]
        assert egress.flushes == []

        # Phase 2 — speech persists past min-speech (200 ms) -> commit.
        await rig.push_hops(*[SPEECH_PROB] * (SPEECH_HOPS_TO_CONFIRM - 1))
        await _until(lambda: bool(egress.flushes), message="the barge-in commit flush")
    finally:
        hold.set()
        await rig.stop()

    # THE assertion: duck, then flush, then resume — in that order and
    # with resume IMMEDIATELY after flush (audio_egress judgment call 9).
    assert egress.barge_in_calls == ["pause", "flush", "resume"]
    flush_at = egress.calls.index("flush")
    assert egress.calls[flush_at + 1] == "resume"

    # The interrupted turn's caption is the truncated played words, not
    # the full generation (§6.3): 155 ms -> char 15 ("gamma" started at
    # 150 ms) -> snapped forward to the word boundary.
    agent_final = rig.messages_of(DC_TYPE_TRANSCRIPT_FINAL, role="agent")[0]
    assert agent_final.payload["text"] == "Alpha beta gamma"
    assert agent_final.payload["turn"] == 1

    # The indicator returns to listening on the commit (docs/13 §4).
    assert rig.states() == ["thinking", "speaking", "listening"]


async def test_barge_in_false_positive_resumes_without_flushing(settings: Settings) -> None:
    """§6.1 phase 2's other branch: speech stops before confirmation, so
    the duck is reversed for free — nothing cancelled, nothing dropped."""
    egress = FakeEgress(auto_play=False)
    rig = Rig(settings, egress=egress)
    hold = asyncio.Event()
    reply = "Alpha beta gamma delta."
    await rig.start()
    try:
        await _speak_until_audio(rig, reply, hold)
        await rig.push_hops(SPEECH_PROB)  # cough onset -> duck
        assert egress.barge_in_calls == ["pause"]
        await rig.push_hops(SILENCE_PROB)  # ...and it was only a cough
        assert egress.barge_in_calls == ["pause", "resume"]
    finally:
        # Unlike the commit cases (where the speaking task is cancelled),
        # this turn runs to completion — so it waits for the queue to
        # actually play out (docs/06 §4: SPEAKING ends at playout, not at
        # synthesis). Let the fake queue drain so teardown isn't a hang.
        egress.set_played(FAKE_TTS_MS_PER_CHAR * len(reply))
        hold.set()
        await rig.stop()

    # Nothing was committed: no flush, and the transcript is untouched
    # (the caption that lands is the full reply, from the normal path).
    assert egress.flushes == []
    assert rig.tts.calls == [(reply, 0)]


async def test_barge_in_commit_is_idempotent_under_debounce(settings: Settings) -> None:
    """§6.5's double-barge-in guard: a second confirmation inside the
    ~100 ms debounce must not flush the new turn's queue."""
    egress = FakeEgress(auto_play=False)
    rig = Rig(settings, egress=egress)
    hold = asyncio.Event()
    reply = "Alpha beta gamma delta."
    await rig.start()
    try:
        await _speak_until_audio(rig, reply, hold)
        egress.set_played(155)
        await rig.push_hops(*[SPEECH_PROB] * SPEECH_HOPS_TO_CONFIRM)
        await _until(lambda: bool(egress.flushes), message="the barge-in commit flush")
        # The tail of the caller's own utterance keeps arriving.
        await rig.push_hops(*[SPEECH_PROB] * SPEECH_HOPS_TO_CONFIRM)
    finally:
        hold.set()
        await rig.stop()

    assert len(egress.flushes) == 1


# --------------------------------------------------------------------------
# docs/06 §9 degradation ladder
# --------------------------------------------------------------------------


async def test_stt_stream_death_reconnects_and_re_asks_once(settings: Settings) -> None:
    """§9's STT row: the worker reconnects immediately, and the lost
    utterance earns exactly one honest re-ask — the brain never runs."""
    rig = Rig(settings, stt_final_timeout_s=5.0)
    await rig.start()
    try:
        # A partial (so the completeness hold fires) but NO final: the
        # turn opens and waits on the tail...
        rig.stt.push(SttPartial(text="I need help with a payment.", ts_ms=100))
        await _until(
            lambda: bool(rig.messages_of(DC_TYPE_TRANSCRIPT_PARTIAL, role="user")),
            message="the user caption partial",
        )
        await rig.drive_endpoint()
        await _until(lambda: "thinking" in rig.states(), message="the THINKING transition")

        # ...and the socket dies underneath it.
        rig.stt.fail(ConnectionError("deepgram socket closed"))
        await _until(
            lambda: bool(rig.messages_of(DC_TYPE_TRANSCRIPT_FINAL, role="agent")),
            message="the re-ask to be spoken",
        )
        await _until(lambda: rig.stt.streams_opened >= 2, message="the STT reconnect")
    finally:
        await rig.stop()

    # Exactly one reconnect, and the re-ask is spoken verbatim.
    assert rig.stt.streams_opened == 2
    assert rig.brain.calls == []  # no turn was opened on a lost utterance
    assert rig.tts.calls == [(REASK_TEXT, 0)]
    agent_final = rig.messages_of(DC_TYPE_TRANSCRIPT_FINAL, role="agent")[0]
    assert agent_final.payload["text"] == REASK_TEXT
    # A lost utterance has no authoritative user caption to freeze.
    assert rig.messages_of(DC_TYPE_TRANSCRIPT_FINAL, role="user") == []


async def test_tts_mid_stream_failure_skips_sentence_and_continues(settings: Settings) -> None:
    """§9's TTS row: a sentence that dies AFTER its first audio byte is
    skipped (no re-synthesis — that would double-speak the prefix) and
    the rest of the reply still plays."""
    rig = Rig(settings)
    rig.brain.script("Alpha one. Beta two. Gamma three.")
    # Sentence 1 yields one chunk, then the stream dies mid-sentence.
    rig.tts.script(
        "Beta two.",
        fake_tts_chunk("Beta two."),
        fail_after=1,
        error=ConnectionError("elevenlabs stream aborted"),
    )
    await rig.start()
    try:
        await _open_turn(rig, "Give me the summary.")
        await _until(
            lambda: bool(rig.messages_of(DC_TYPE_TRANSCRIPT_FINAL, role="agent")),
            message="the reply to finish",
        )
    finally:
        await rig.stop()

    # All three sentences were dispatched; the failure did not abort the
    # turn or skip the sentence AFTER the failed one.
    assert rig.tts.calls == [("Alpha one.", 0), ("Beta two.", 1), ("Gamma three.", 2)]
    assert [c.sentence_no for c in rig.egress.chunks] == [0, 1, 2]
    # The reply completed normally, captions and all.
    assert rig.states() == ["thinking", "speaking", "listening"]
    agent_final = rig.messages_of(DC_TYPE_TRANSCRIPT_FINAL, role="agent")[0]
    assert agent_final.payload["text"] == "Alpha one. Beta two. Gamma three."


async def test_data_channel_failure_never_breaks_the_turn(settings: Settings) -> None:
    """Judgment call 8: the `ctx` channel not being open degrades
    captions, loudly, but the call keeps talking."""
    rig = Rig(settings)
    rig.brain.script("Still speaking.")

    def explode(_message: DataChannelMessage) -> None:
        raise RuntimeError("ctx data channel is not open")

    rig.worker._send_message = explode  # type: ignore[assignment]
    await rig.start()
    try:
        rig.stt.push(SttFinal(text="Are you there?", ts_ms=200))
        await asyncio.sleep(0)
        await rig.drive_endpoint()
        await _until(lambda: bool(rig.tts.calls), message="the reply to be synthesized")
    finally:
        await rig.stop()

    assert rig.brain.calls == ["Are you there?"]
    assert rig.tts.calls == [("Still speaking.", 0)]
    assert len(rig.egress.chunks) == 1


# --------------------------------------------------------------------------
# Span accounting (docs/04 §7.2's frozen table rows this layer owns)
# --------------------------------------------------------------------------


def _attrs(span: Any) -> dict[str, Any]:
    return dict(span.attributes or {})


def _uncache_worker_tracer() -> None:
    """`ProxyTracer` resolves and then PERMANENTLY caches `_real_tracer`
    the first time a span is opened through it (verified in
    opentelemetry-api's source; tests/e2e/test_canonical_conversation.py
    judgment call #4 documents the same hazard for ConversationManager).
    worker.py's `tracer = get_tracer(__name__)` is a module-level object,
    so without this the provider swap below would be silently ignored."""
    if hasattr(worker_module.tracer, "_real_tracer"):
        worker_module.tracer._real_tracer = None  # type: ignore[attr-defined]


@pytest.fixture
def worker_spans(settings: Settings) -> Iterator[InMemorySpanExporter]:
    """A provider + exporter owned by THIS module, rather than conftest's
    session-wide `span_exporter`.

    Reason, found by running the full suite rather than this file alone:
    `tests/obs/test_tracing.py` resets the global `_TRACER_PROVIDER` to
    `None` (plus its `Once()` guard) after every one of its tests, and
    `tests/voice/` sorts after `tests/obs/` — so by the time this module
    runs, `get_tracer_provider()` is the API's default proxy, which has
    no `force_flush()`. Every pre-existing `span_exporter` consumer sorts
    BEFORE tests/obs and so never hit this. Owning the reset here
    (tests/obs/test_tracing.py's established pattern) makes these two
    tests order-independent instead of quietly order-dependent."""
    otel_trace._TRACER_PROVIDER = None
    otel_trace._TRACER_PROVIDER_SET_ONCE = Once()
    _uncache_worker_tracer()
    exporter = InMemorySpanExporter()
    setup_observability(settings, exporter=exporter)
    yield exporter
    otel_trace._TRACER_PROVIDER = None
    otel_trace._TRACER_PROVIDER_SET_ONCE = Once()
    _uncache_worker_tracer()


async def test_span_accounting_turn_and_stt_final(
    settings: Settings, worker_spans: InMemorySpanExporter
) -> None:
    """One `turn` span (carrying `endpoint_ms`, docs/06 §4.1) and one
    `stt.final` span per turn — the two rows docs/04 §7.2 assigns to
    VoiceAgentWorker, with exactly the frozen attributes."""
    rig = Rig(settings)
    rig.brain.script("Short reply.")
    await rig.start()
    try:
        await _open_turn(rig, "One question?")
        await _until(
            lambda: bool(rig.messages_of(DC_TYPE_TRANSCRIPT_FINAL, role="agent")),
            message="the turn to finish",
        )
    finally:
        await rig.stop()

    cast(SdkTracerProvider, otel_trace.get_tracer_provider()).force_flush()
    spans = worker_spans.get_finished_spans()

    # The brain here is a fake and opens no spans of its own, so the
    # worker's two rows are the entire trace for this turn.
    turn_spans = [s for s in spans if s.name == SPAN_TURN]
    stt_spans = [s for s in spans if s.name == SPAN_STT_FINAL]
    assert len(turn_spans) == 1
    assert len(stt_spans) == 1
    assert len(spans) == 2

    turn_attrs = _attrs(turn_spans[0])
    assert turn_attrs["session_id"] == "sess-worker"
    assert turn_attrs["turn_no"] == 1
    # The §5.3 row-3 endpoint waited 256 ms of trailing silence (8 hops).
    assert turn_attrs["endpoint_ms"] == SILENCE_HOPS_TO_ENDPOINT * 32
    assert turn_attrs["interrupted"] is False
    assert "turn_ms" in turn_attrs

    stt_attrs = _attrs(stt_spans[0])
    assert stt_attrs["is_endpoint"] is True  # not the §5.3 row-4 force cap
    assert stt_attrs["text_len"] == len("One question?")
    assert "stt_ms" in stt_attrs


async def test_interrupted_turn_span_records_the_interruption(
    settings: Settings, worker_spans: InMemorySpanExporter
) -> None:
    egress = FakeEgress(auto_play=False)
    rig = Rig(settings, egress=egress)
    hold = asyncio.Event()
    reply = "Alpha beta gamma delta."
    await rig.start()
    try:
        await _speak_until_audio(rig, reply, hold)
        egress.set_played(FAKE_TTS_MS_PER_CHAR * 16)
        await rig.push_hops(*[SPEECH_PROB] * SPEECH_HOPS_TO_CONFIRM)
        await _until(lambda: bool(egress.flushes), message="the barge-in commit flush")
    finally:
        hold.set()
        await rig.stop()

    cast(SdkTracerProvider, otel_trace.get_tracer_provider()).force_flush()
    turn_spans = [s for s in worker_spans.get_finished_spans() if s.name == SPAN_TURN]
    assert len(turn_spans) == 1
    assert _attrs(turn_spans[0])["interrupted"] is True
