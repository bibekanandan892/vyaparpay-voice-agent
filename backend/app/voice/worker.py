"""VoiceAgentWorker — the per-call turn orchestrator (docs/06 §1.1's table
row): drains the VAD fan-out into `VadEndpointer`, opens a turn per
`Endpoint`, collects the STT final, calls the brain, and hands the reply
to `SpeechDispatcher` (app/voice/speech.py: chunker → per-sentence TTS →
paced egress). Emits the docs/13 §4 data-channel envelopes
(`transcript.partial`/`transcript.final`/`agent.state`) and owns barge-in
duck-then-commit (docs/06 §6) plus the §9 rows assigned to this layer
(STT reconnect + one honest re-ask — transport half in
app/voice/stt_supervisor.py; TTS mid-stream skip lives with dispatch in
speech.py).

Turn state machine (docs/06 §4/§6; the wire vocabulary is `AgentState`):
LISTENING → (Endpoint) THINKING → (first TTS audio) SPEAKING →
(playout complete | barge-in commit) LISTENING. TRANSCRIBING is
ConversationManager's internal state, collapsed into "thinking" on the
wire (domain judgment call 6). Cancellation ownership: the barge-in
commit (VAD-loop frame handler) cancels the SPEAKING subtask; the call
session cancels the whole `run()` task at teardown; nothing else cancels
anything.

Judgment calls, numbered so review argues with the reasoning:

1. **Duck detection reads the VadModel's probability through a recording
   proxy, never a second `prob()` call.** Silero is stateful (rolling
   context + recurrent state), so calling `prob()` twice per frame would
   corrupt it; `VadEndpointer` deliberately emits only the two policy
   events (its judgment call 2) and leaves raw per-frame activity to
   this class. `_ProbeVad` wraps the injected model, the endpointer
   calls it exactly once per hop, and the worker reads the recorded
   probability afterwards.
2. **Barge-in commit is exactly: flush → RESUME → truncate → cancel.**
   `AudioEgress.flush()` deliberately does NOT resume (egress judgment
   call 9) — the `resume()` here is load-bearing: without it the next
   reply plays into a paused queue as silence. The commit handler runs
   inline in the VAD loop (the flush must not wait behind a task hop),
   then cancels the speaking task; the turn task swallows exactly that
   cancellation (`turn.interrupted` is set first) and closes its span
   with `interrupted=True`.
3. **A committed barge-in truncates the WIRE transcript only.** docs/06
   §6.3 also wants the session-memory assistant turn truncated with an
   `[interrupted]` marker, but the Phase-2 `ConversationManager` appends
   the full reply inside `on_stt_final()` before this worker ever speaks
   a word, and exposes no post-hoc edit seam. The truncated played-words
   text goes out as `transcript.final` (docs/13 §4's requirement) and
   into the `worker.barge_in_committed` log; the session-memory
   truncation is flagged for the Batch-6 streaming-brain seam
   (`ReplySink.on_turn_complete` is where it belongs).
4. **Speech during THINKING does not abandon the turn.** docs/13 §4's
   diagram shows Thinking → Listening on "turn abandoned", but with the
   Phase-2 brain the reply is non-cancellable mid-generation (Batch 6's
   task) AND already appended to the session transcript — discarding it
   unspoken would make the record claim Asha said something she never
   voiced, the exact §6.3 corruption. So an `Endpoint` that fires while
   a turn is in flight is queued (latest wins) and opens the next turn
   when the current one ends; the caller can barge in the moment audio
   starts.
5. **The `turn` span opens here, at the endpoint (docs/06 §4.1), with
   `endpoint_ms`/`interrupted`/`turn_ms`; the frozen Phase-2
   `ConversationManager` still opens its own `turn` span inside
   `on_stt_final`, which now nests under this one.** Two spans named
   `turn` per turn is a known, documented duplicate until Batch 6 moves
   root-span ownership out of the brain; tests count this worker's span
   by its `endpoint_ms` attribute, not by name alone.
6. **Re-ask policy (§9): spoken only when an utterance was actually
   lost** — a turn was already waiting on its final when the stream
   died (`SttStreamDied` via the supervisor's `on_stream_died` hook).
   A death with no turn in flight reconnects silently. An endpoint
   whose final times out on a LIVE stream falls back to the latest
   partial (logged) — better than asking the caller to repeat words we
   heard.
7. **The STT final, when it arrives before our endpoint, is held in a
   one-slot buffer and also fed to the endpointer as a partial.**
   Deepgram's own endpointing (250 ms) routinely beats Silero's
   conjunction rule, so final-before-Endpoint is the COMMON ordering;
   the buffer makes turn-open consume it instantly, and feeding its
   text to `on_partial` keeps the completeness hold looking at the
   authoritative text rather than a stale interim.
8. **Data-channel sends are best-effort with a warning log.** PeerSession
   fails loudly when the channel is not open; captions and state dots
   degrade (audio is the product), but never silently — every drop
   logs. `seq` still increments on a failed send: the client never
   gap-checks the server stream (docs/13 §4).
9. **User captions are throttled on the STT media clock (~5/s → 200 ms
   min gap, docs/13 §4); agent captions are cumulative per sentence**
   at dispatch time — each partial supersedes the previous wholesale
   (the overlay replaces on (turn, role)), so a per-sentence-only
   payload would erase the caption of every earlier sentence.

Docs: docs/06 §4-§7/§9, docs/04 §7.2 (span rows), docs/13 §4.
Tests: tests/voice/test_worker.py, tests/voice/test_truncation.py,
tests/e2e/test_voice_pipeline_e2e.py (fake-peer, CI's docker-gated job).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final, Literal, Protocol

from app.config import Settings
from app.domain.types import EndReason
from app.domain.voice import (
    DC_TYPE_AGENT_STATE,
    DC_TYPE_TRANSCRIPT_FINAL,
    DC_TYPE_TRANSCRIPT_PARTIAL,
    AgentState,
    AgentStateMsg,
    DataChannelMessage,
    SttEvent,
    SttFinal,
    SttPartial,
    SttProvider,
    TranscriptFinal,
    TranscriptPartial,
    TtsProvider,
    VadModel,
)
from app.obs.logging import get_logger
from app.obs.tracing import SPAN_STT_FINAL, SPAN_TURN, get_tracer, safe_set_attribute
from app.voice.audio_ingress import AudioIngress
from app.voice.speech import EgressLike, SpeechDispatcher, Turn
from app.voice.stt_supervisor import SttSupervisor
from app.voice.truncation import truncate_reply
from app.voice.vad_endpointer import Endpoint, SpeechStart, VadEndpointer

log = get_logger(__name__)
tracer = get_tracer(__name__)

# docs/06 §6.5: ignore barge-in signals briefly after a commit so the tail
# of the utterance that just committed cannot re-trigger on the new turn.
BARGE_IN_DEBOUNCE_MS: Final = 100
# Judgment call 9: docs/13 §4's ~5/s user-caption throttle.
PARTIAL_THROTTLE_MS: Final = 200
# Judgment call 6: how long past the endpoint to wait for the STT final
# (p95 is 150 ms, docs/06 §4) before degrading to the latest partial.
STT_FINAL_TIMEOUT_S: Final = 2.0
# docs/06 §9's honest re-ask, verbatim.
REASK_TEXT: Final = "Sorry, I missed that — could you say it again?"

Role = Literal["user", "agent"]


class Brain(Protocol):
    """The brain seam this worker drives — `ConversationManager`
    satisfies it as-is (docs/05 §1.1's transport-blind boundary).

    The two `record_*` methods carry billable media usage to the brain,
    which holds this call's `CostTracker` (app/voice/run.py builds one per
    call and hands it to `ConversationManager`): STT is priced per
    audio-minute and TTS per character (docs/16 §5), and neither quantity
    is visible from inside the brain. They pass usage counts only — no
    transport detail — so the boundary above still holds."""

    async def on_stt_final(self, text: str) -> str: ...
    def record_stt_audio(self, seconds: float) -> None: ...
    def record_tts_text(self, characters: int) -> None: ...


@dataclass(frozen=True)
class BuiltBrain:
    """What `brain_factory()` hands back: the turn machine plus the
    post-call hook that runs `CostTracker.finalize()` then
    `SessionManager.end()` for this call (docs/05 §3.1, docs/09 §8),
    exactly once, from `run()`'s `finally` below.

    `on_call_ended` travels bundled with `brain`, not as a separately
    injected constructor parameter, because both are built together —
    and must stay together — inside `app/voice/run.py`'s
    `_make_brain_factory`: that closure constructs this call's own
    `CostTracker` right before building the `ConversationManager` that
    holds it, and `CostTracker.finalize()` reads *that specific
    instance's* in-memory accumulators, not a fresh lookup keyed by
    `session_id` (cost_tracker.py has no such lookup — the running
    total only ever lives on the one instance that recorded it). Threading
    two independently-obtained callables down to this worker would risk
    a `on_call_ended` closing over a different `CostTracker` than the one
    `brain` is actually billing turns to.

    Deliberately NOT a member of the `Brain` Protocol itself: finalize/end
    are session-lifecycle concerns owned by `SessionManager`/`CostTracker`,
    one level above the agent-brain (docs/05 §1.1's transport-blind
    boundary is about audio/transport detail, but the boundary this
    class respects is about ownership — `ConversationManager` has never
    known about `SessionManager.end()` and should not start now)."""

    brain: Brain
    on_call_ended: Callable[[EndReason], Awaitable[None]]


class SttStreamDied(RuntimeError):
    """The provider stream failed while a turn was waiting on its final."""


class _ProbeVad:
    """Judgment call 1: records each probability on its way to the
    endpointer so the duck logic reads, never re-computes, it."""

    def __init__(self, inner: VadModel) -> None:
        self._inner = inner
        self.last_prob = 0.0

    def prob(self, frame_30ms: bytes) -> float:
        self.last_prob = self._inner.prob(frame_30ms)
        return self.last_prob


def _now_epoch_ms() -> int:
    return int(time.time() * 1000)


class VoiceAgentWorker:
    """One instance per call. `run()` owns the task group (VAD loop, STT
    supervisor, turn tasks) until the ingress fan-outs end (peer closed)."""

    def __init__(
        self,
        session_id: str,
        *,
        ingress: AudioIngress,
        egress: EgressLike,
        stt: SttProvider,
        tts: TtsProvider,
        vad: VadModel,
        brain_factory: Callable[[], Awaitable[BuiltBrain]],
        send_message: Callable[[DataChannelMessage], None],
        settings: Settings,
        now_ms: Callable[[], int] = _now_epoch_ms,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        stt_final_timeout_s: float = STT_FINAL_TIMEOUT_S,
    ) -> None:
        self._session_id = session_id
        self._ingress = ingress
        self._egress = egress
        self._probe = _ProbeVad(vad)
        self._endpointer = VadEndpointer(self._probe, settings)
        self._vad_threshold = settings.vad_threshold
        self._brain_factory = brain_factory
        self._send_message = send_message
        self._now_ms = now_ms
        self._stt_final_timeout_s = stt_final_timeout_s
        self._supervisor = SttSupervisor(
            session_id,
            stt=stt,
            ingress=ingress,
            on_event=self._on_stt_event,
            on_stream_died=self._fail_final_waiter,
            on_audio_seconds=self._record_stt_audio,
        )
        self._speech = SpeechDispatcher(
            session_id,
            tts=tts,
            egress=egress,
            sleep=sleep,
            on_agent_partial=self._send_agent_partial,
            on_speaking_started=lambda turn_no: self._set_state(AgentState.SPEAKING, turn_no),
            on_agent_final=lambda turn_no, text: self._send_transcript_final(
                turn_no, "agent", text
            ),
            on_tts_chars=self._record_tts_text,
        )
        self._brain: Brain | None = None
        self._tg: asyncio.TaskGroup | None = None
        self._state = AgentState.LISTENING
        self._turn_no = 0
        self._seq = 0
        self._current_turn: Turn | None = None
        self._pending_endpoint: Endpoint | None = None
        self._ducked = False
        self._debounce_until_ms = 0
        self._final_waiter: asyncio.Future[SttFinal] | None = None
        self._pending_final: SttFinal | None = None
        self._latest_partial = ""
        self._last_partial_sent_ms: int | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Run the call's loops until the ingress fan-outs end (the peer
        closed ingress) and every in-flight turn task finished, then run
        this call's post-call finalize+end exactly once (docs/05 §3.1,
        docs/09 §8) — this is the convergence point every hangup path
        (client bye, transport EOF/error, operator DELETE, worker crash)
        reduces to, because closing the peer ends the ingress fan-outs
        gracefully and `run()` is a single task that only ever executes
        once per call: `on_call_ended` firing here cannot race a second
        invocation of itself, no matter how many times a racing close
        path calls `CallSession.close()`/`peer.close()` (both idempotent,
        signaling.py judgment call 8).

        Reason defaults to HANGUP — every graceful teardown (the ingress
        fan-outs simply ending) takes this path, client- and
        operator-initiated alike; docs/13 §2.2 itself calls the operator
        DELETE an "explicit hang-up". It flips to ERROR only when a real
        exception escapes the task group — a genuine worker crash, not a
        cancellation from being torn down (`asyncio.CancelledError` is a
        `BaseException`, not an `Exception`, so it is not caught here and
        the reason stays HANGUP) — because turns that already ran before
        the crash spent real STT/TTS/LLM money that still belongs in
        `call_costs`, and `SessionManager.end()` still needs to flip
        `conversations.state` so the call does not hang as `in_call`
        forever."""
        built = await self._brain_factory()
        self._brain = built.brain
        log.info("worker.started", session_id=self._session_id)
        reason = EndReason.HANGUP
        try:
            async with asyncio.TaskGroup() as tg:
                self._tg = tg
                tg.create_task(self._vad_loop(), name=f"worker:vad:{self._session_id}")
                tg.create_task(self._supervisor.run(), name=f"worker:stt:{self._session_id}")
        except Exception:
            reason = EndReason.ERROR
            raise
        finally:
            self._tg = None
            log.info(
                "worker.stopped",
                session_id=self._session_id,
                turns=self._turn_no,
                reason=reason.value,
            )
            try:
                await built.on_call_ended(reason)
            except Exception:
                log.exception(
                    "worker.post_call_finalize_failed",
                    session_id=self._session_id,
                    reason=reason.value,
                )
                raise

    # ------------------------------------------------------------------
    # Billable media usage -> the brain's per-call cost ledger
    # ------------------------------------------------------------------

    def _require_brain(self) -> Brain:
        """`run()` sets `_brain` before creating any task, and every
        caller below runs inside one of those tasks — so a None here is a
        wiring bug, not a race, and must not be swallowed into a silently
        free call."""
        brain = self._brain
        if brain is None:  # pragma: no cover - unreachable via run()
            raise RuntimeError("worker used the brain before it was built")
        return brain

    def _record_stt_audio(self, seconds: float) -> None:
        self._require_brain().record_stt_audio(seconds)

    def _record_tts_text(self, characters: int) -> None:
        self._require_brain().record_tts_text(characters)

    # ------------------------------------------------------------------
    # VAD loop — endpointing + barge-in (docs/06 §5/§6)
    # ------------------------------------------------------------------

    async def _vad_loop(self) -> None:
        async for frame in self._ingress.vad_frames():
            event = self._endpointer.on_frame(frame)
            frame_end_ms = frame.ts_ms + round(frame.samples * 1000 / frame.sample_rate)
            self._handle_barge_in(event, frame_end_ms)
            if isinstance(event, Endpoint):
                self._on_endpoint(event)

    def _handle_barge_in(self, event: SpeechStart | Endpoint | None, frame_end_ms: int) -> None:
        """docs/06 §6.1: duck on the first speech frame during SPEAKING,
        resume on a pre-confirmation silence frame (false positive),
        commit on SpeechStart (min-speech confirmed)."""
        turn = self._current_turn
        if turn is None or not turn.speaking or turn.interrupted:
            return
        if frame_end_ms < self._debounce_until_ms:
            return  # §6.5's post-commit debounce
        is_speech = self._probe.last_prob >= self._vad_threshold
        if is_speech and not self._ducked:
            self._ducked = True
            self._egress.pause()
            log.info("worker.barge_in_ducked", session_id=self._session_id, turn=turn.turn_no)
        elif not is_speech and self._ducked:
            # §6.1's free false-positive recovery: nothing was cancelled.
            self._ducked = False
            self._egress.resume()
            log.info("worker.barge_in_resumed", session_id=self._session_id, turn=turn.turn_no)
        if isinstance(event, SpeechStart):
            self._commit_barge_in(turn, frame_end_ms)

    def _commit_barge_in(self, turn: Turn, frame_end_ms: int) -> None:
        """Judgment call 2 — the §6.1 commit, in strict order."""
        if turn.interrupted:
            return  # §6.5: idempotent against double barge-in
        turn.interrupted = True
        result = self._egress.flush()
        # THE sharp edge (egress judgment call 9): flush() deliberately
        # does not resume; without this the next reply plays silence.
        self._egress.resume()
        self._ducked = False
        self._debounce_until_ms = frame_end_ms + BARGE_IN_DEBOUNCE_MS
        played_turn_ms = max(0, result.played_ms - turn.baseline_played_ms)
        truncated = truncate_reply(turn.sentences, played_turn_ms)
        self._send_transcript_final(turn.turn_no, "agent", truncated)
        turn.final_sent = True
        self._set_state(AgentState.LISTENING, turn.turn_no)
        turn.listening_sent = True
        if turn.speaking_task is not None and not turn.speaking_task.done():
            turn.speaking_task.cancel()
        log.info(
            "worker.barge_in_committed",
            session_id=self._session_id,
            turn=turn.turn_no,
            interrupted=True,
            played_ms=played_turn_ms,
            dropped_ms=result.dropped_ms,
            truncated_chars=len(truncated),
        )

    def _on_endpoint(self, endpoint: Endpoint) -> None:
        if self._current_turn is not None:
            self._pending_endpoint = endpoint  # judgment call 4: latest wins
            return
        self._start_turn(endpoint)

    def _start_turn(self, endpoint: Endpoint) -> None:
        if self._tg is None:
            raise RuntimeError("VoiceAgentWorker turn started outside run()")
        self._tg.create_task(
            self._run_turn(endpoint), name=f"worker:turn:{self._session_id}:{self._turn_no + 1}"
        )

    # ------------------------------------------------------------------
    # Turn pipeline (docs/06 §4)
    # ------------------------------------------------------------------

    async def _run_turn(self, endpoint: Endpoint) -> None:
        self._turn_no += 1
        turn = Turn(turn_no=self._turn_no, baseline_played_ms=self._egress.played_ms)
        self._current_turn = turn
        started_ms = self._now_ms()
        try:
            # Judgment call 5: the endpoint decision opens the `turn` span
            # (docs/06 §4.1); the Phase-2 brain's own `turn` span nests.
            with tracer.start_as_current_span(SPAN_TURN) as span:
                safe_set_attribute(span, "session_id", self._session_id)
                safe_set_attribute(span, "turn_no", turn.turn_no)
                safe_set_attribute(span, "endpoint_ms", endpoint.endpoint_ms)
                self._set_state(AgentState.THINKING, turn.turn_no)
                text = await self._collect_final(endpoint)
                if text is None:
                    # §9: the utterance is lost — one honest re-ask.
                    await self._run_speaking(turn, REASK_TEXT)
                else:
                    self._send_transcript_final(turn.turn_no, "user", text)
                    brain = self._brain
                    if brain is None:  # pragma: no cover - run() sets it first
                        raise RuntimeError("worker turn ran before brain was built")
                    reply = await brain.on_stt_final(text)
                    await self._run_speaking(turn, reply)
                safe_set_attribute(span, "interrupted", turn.interrupted)
                safe_set_attribute(span, "turn_ms", self._now_ms() - started_ms)
        finally:
            if self._ducked:  # safety net: never leave the egress paused
                self._ducked = False
                self._egress.resume()
            if not turn.listening_sent:
                self._set_state(AgentState.LISTENING, turn.turn_no)
            self._current_turn = None
            pending, self._pending_endpoint = self._pending_endpoint, None
            if pending is not None:
                self._start_turn(pending)

    async def _run_speaking(self, turn: Turn, reply_text: str) -> None:
        """Run the speaking phase as a cancellable subtask — the barge-in
        commit's cancel target (judgment call 2)."""
        task = asyncio.create_task(
            self._speech.speak(turn, reply_text),
            name=f"worker:speak:{self._session_id}:{turn.turn_no}",
        )
        turn.speaking_task = task
        try:
            await task
        except asyncio.CancelledError:
            if not turn.interrupted:
                raise  # not ours: the worker itself is being torn down
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _collect_final(self, endpoint: Endpoint) -> str | None:
        """The `stt.final` span (docs/04 §7.2: this layer's row): consume
        the buffered final (judgment call 7) or wait for it; None means
        the utterance is lost and the turn degrades to the re-ask."""
        with tracer.start_as_current_span(SPAN_STT_FINAL) as span:
            safe_set_attribute(span, "is_endpoint", not endpoint.forced)
            started_ms = self._now_ms()
            if self._pending_final is not None:
                text: str | None = self._pending_final.text
                self._pending_final = None
            else:
                text = await self._await_final()
            safe_set_attribute(span, "stt_ms", self._now_ms() - started_ms)
            safe_set_attribute(span, "text_len", len(text) if text is not None else 0)
            return text

    async def _await_final(self) -> str | None:
        waiter: asyncio.Future[SttFinal] = asyncio.get_running_loop().create_future()
        self._final_waiter = waiter
        try:
            final = await asyncio.wait_for(waiter, timeout=self._stt_final_timeout_s)
            return final.text
        except SttStreamDied:
            log.warning("worker.stt_final_lost", session_id=self._session_id)
            return None
        except TimeoutError:
            # Judgment call 6: live stream, no final — the latest partial
            # is closer to the truth than a re-ask for words we heard.
            if self._latest_partial:
                log.warning(
                    "worker.stt_final_timeout_partial_fallback",
                    session_id=self._session_id,
                    text_len=len(self._latest_partial),
                )
                return self._latest_partial
            log.warning("worker.stt_final_timeout", session_id=self._session_id)
            return None
        finally:
            self._final_waiter = None

    def _fail_final_waiter(self) -> None:
        if self._final_waiter is not None and not self._final_waiter.done():
            self._final_waiter.set_exception(SttStreamDied("STT stream died mid-turn"))

    # ------------------------------------------------------------------
    # STT event routing (fed by SttSupervisor)
    # ------------------------------------------------------------------

    def _on_stt_event(self, event: SttEvent) -> None:
        if isinstance(event, SttPartial):
            self._endpointer.on_partial(event)
            self._latest_partial = event.text
            self._send_user_partial(event)
            return
        # Judgment call 7: the final also drives the completeness hold,
        # and buffers when it beats our endpoint.
        self._endpointer.on_partial(SttPartial(text=event.text, ts_ms=event.ts_ms))
        self._latest_partial = ""
        self._last_partial_sent_ms = None
        if self._final_waiter is not None and not self._final_waiter.done():
            self._final_waiter.set_result(event)
        else:
            self._pending_final = event

    # ------------------------------------------------------------------
    # Data-channel emission (docs/13 §4; judgment calls 8-9)
    # ------------------------------------------------------------------

    def _send_user_partial(self, partial: SttPartial) -> None:
        last = self._last_partial_sent_ms
        if last is not None and partial.ts_ms - last < PARTIAL_THROTTLE_MS:
            return
        self._last_partial_sent_ms = partial.ts_ms
        # The utterance being transcribed belongs to the turn awaiting its
        # final if one is open, otherwise to the turn it will become.
        turn_no = self._turn_no if self._final_waiter is not None else self._turn_no + 1
        self._send_dc(
            DC_TYPE_TRANSCRIPT_PARTIAL,
            TranscriptPartial(turn=turn_no, role="user", text=partial.text),
        )

    def _send_agent_partial(self, turn_no: int, text: str) -> None:
        self._send_dc(
            DC_TYPE_TRANSCRIPT_PARTIAL, TranscriptPartial(turn=turn_no, role="agent", text=text)
        )

    def _send_transcript_final(self, turn_no: int, role: Role, text: str) -> None:
        self._send_dc(
            DC_TYPE_TRANSCRIPT_FINAL, TranscriptFinal(turn=turn_no, role=role, text=text)
        )

    def _set_state(self, state: AgentState, turn_no: int) -> None:
        self._state = state
        self._send_dc(DC_TYPE_AGENT_STATE, AgentStateMsg(state=state, turn=turn_no))

    def _send_dc(
        self, type_: str, payload: TranscriptPartial | TranscriptFinal | AgentStateMsg
    ) -> None:
        self._seq += 1
        message = DataChannelMessage(
            type=type_,
            seq=self._seq,
            ts=self._now_ms(),
            payload=payload.model_dump(mode="json"),
        )
        try:
            self._send_message(message)
        except Exception:
            # Judgment call 8: captions degrade, loudly, never fatally.
            log.warning(
                "worker.data_channel_send_failed",
                session_id=self._session_id,
                type=type_,
                seq=message.seq,
            )


__all__ = [
    "BARGE_IN_DEBOUNCE_MS",
    "PARTIAL_THROTTLE_MS",
    "REASK_TEXT",
    "STT_FINAL_TIMEOUT_S",
    "Brain",
    "BuiltBrain",
    "SttStreamDied",
    "VoiceAgentWorker",
]
