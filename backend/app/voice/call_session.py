"""CallSession — the per-call wiring the signaling server's PeerFactory
constructs (satisfying `PeerSessionLike`): PeerSession + AudioIngress +
AudioEgress, and — when `CallDeps` is provided — the real
`VoiceAgentWorker` turn machine over injected STT/TTS/VAD/brain. This is
T5.1's replacement for run.py's T3.1 `PlaceholderCallSession`; the
placeholder survives only as a deps-less mode of this class (run.py keeps
the name as a thin shim because tests/voice/test_run.py pins its
contract).

Judgment calls, flagged per house style:

1. **Lazy egress start on offer, carried forward verbatim from T3.1's
   review-HIGH fix**: starting `AudioEgress.run()` at construction would
   run the self-driven 20 ms cadence for however long the client takes
   to send its offer, overflowing `_EgressTrack`'s ~1 s cap with
   pre-connect silence. `_egress_task` starts on the FIRST offer only —
   an ICE-restart re-offer must not start a second pacer.
2. **`CallDeps` carries provider INSTANCES but a per-call VAD factory.**
   DeepgramStt/ElevenLabsTts are process-long by their own contract (one
   `stream()`/`synthesize()` call per call/sentence); SileroVad is
   inherently per-stream stateful (its judgment call 2: "one SileroVad
   per uplink audio stream, never shared"), so it must be constructed
   fresh per call. The brain factory is async and takes the session_id —
   the real one awaits `SessionManager.attach()` before building a
   `ConversationManager`, and returns it bundled with this call's
   post-call finalize+end hook as a `BuiltBrain` (worker.py) — the two
   travel together because both close over the same per-call
   `CostTracker` (post-call-pipeline-wiring task).
3. **`deps=None` preserves T3.1's placeholder behavior exactly** (no-op
   drains on both fan-outs so the bounded queues never warn-log): the
   worker process must keep serving transport-only calls in environments
   with no voice keys, and test_run.py's pinned lazy-egress contract
   keeps covering the shared wiring either way.
4. **The worker task is created at construction, not on offer.** Its
   loops block on the ingress fan-outs, which stay empty until media
   flows, so an early start costs nothing — and captions for the very
   first utterance must not race the offer handshake. (The brain build
   inside `run()` overlaps ICE/DTLS setup — the docs/06 §4.2 prefetch
   window — instead of adding to the first turn's latency.)
5. **Teardown order converges with signaling's paths** (same shape as
   T3.1): peer first (ends the uplink drain, closes ingress, which ends
   the worker's fan-out loops), then stop the paced egress, then await
   everything with a timeout — anything still pending is cancelled
   loudly, never leaked. A worker turn mid-brain-call at hang-up gets
   `_CLOSE_TIMEOUT_S` to finish before the cancel. **This budget covers
   the TURN LOOP only — see judgment call 7 for why the post-call
   finalize phase is deliberately NOT bounded by it, and never cancelled
   at all.**
6. **`ContextDispatcher` (Phase-4 T4, `app/voice/context_dispatch.py`)
   wiring is independent of `deps`.** `snapshot_ingestor`/`event_log` are
   their own optional constructor pair — ctx.* capture has nothing to do
   with STT/TTS/brain availability, so even placeholder (deps-less) calls
   get context ingestion when the process was given a pair. Both-or-
   neither: supplying exactly one is a caller bug and raises immediately.
   The dispatcher's `send_data_channel` callback closes over `self._peer`
   rather than being passed a bound method directly, because `PeerSession`
   needs `on_data` AT construction (its own frozen seam) while the
   dispatcher needs `PeerSession.send_data_channel` — a message can only
   ever reach the dispatcher after `__init__` returns and `self._peer` is
   set, so the closure is safe despite the apparent forward reference.
   Its task joins `self._tasks` exactly like the worker/drain tasks, so
   the bounded-wait-then-cancel loop in `close()` already covers it —
   PROVIDED `close()` also calls `ContextDispatcher.close()` (its own
   judgment call 7) right after `self._peer.close()`, since — unlike the
   ingress-fan-out drains — the dispatcher's queue has no other "no more
   messages" signal and would otherwise never finish before
   `_CLOSE_TIMEOUT_S` expires on every single call.
7. **`close()` separately awaits `VoiceAgentWorker.finalize_task` AFTER
   the bounded-wait-then-cancel loop above, with its own budget
   (`_FINALIZE_TIMEOUT_S`) — and never cancels it.** CRITICAL fix
   (2026-08-07, second review pass, same day as judgment call 5's
   original comment): worker.py shields `on_call_ended` (finalize+end)
   from `_CLOSE_TIMEOUT_S`'s cancel so a slow finalize is never
   interrupted mid-write — but that means `run()`'s own task can now
   finish (as cancelled) BEFORE finalize actually has, so `close()`
   returning the moment `run()`'s task is done no longer implies
   finalize is done. That distinction matters because
   `SignalingServer.handle_connection`'s finally (signaling.py judgment
   call 9) frees this call's `_active[session_id]` slot only after
   `close()` returns — a reconnect racing a freed-too-early slot builds
   a SECOND `CallSession`/`VoiceAgentWorker`/`CostTracker` for a
   session_id whose finalize hasn't actually finished, corrupting
   `call_costs` (last-write-wins) or colliding on `conversation_turns`
   primary keys — the exact failure mode a prior fix (signaling.py's own
   comment) already closed once. `close()` re-closes it here by reading
   `worker.finalize_task` directly (exposed for exactly this) and
   waiting for it — not cancelling it if `_FINALIZE_TIMEOUT_S` elapses,
   only logging loudly, because the one invariant that must never break
   again is "this write is never interrupted."

Docs: docs/06 §1.1 (the worker's row), docs/04 §4 (DI split).
Tests: tests/voice/test_run.py (shared wiring, via the shim),
tests/voice/test_context_dispatch.py (dispatcher wiring),
tests/e2e/test_voice_pipeline_e2e.py (full stack with fakes).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import Final

from app.config import Settings
from app.context.event_log import EventLog
from app.context.snapshot_ingestor import SnapshotIngestor
from app.domain.voice import DataChannelMessage, IceServer, SttProvider, TtsProvider, VadModel
from app.obs.logging import get_logger
from app.voice.audio_egress import AudioEgress
from app.voice.audio_ingress import AudioIngress
from app.voice.context_dispatch import ContextDispatcher
from app.voice.peer_session import PeerSession, SendSignal
from app.voice.worker import BuiltBrain, VoiceAgentWorker

log = get_logger(__name__)

_CLOSE_TIMEOUT_S: Final = 5.0

# Judgment call 7. Generous on purpose, and NOT the same knob as
# _CLOSE_TIMEOUT_S: it bounds `close()`'s own wait for
# VoiceAgentWorker.finalize_task, which it never cancels on expiry (only
# logs). Sized against summarizer.py's own `_FOLD_TIMEOUT_S` (45.0s, the
# real bound on ONE LLM summarization call) plus headroom for the two DB
# writes (CostTracker.finalize, SessionManager.end) either side of it —
# on_call_ended can trigger at most one such fold (session_manager.py's
# end() -> fold_pending(), for whatever tail turns summarizer.drain()
# didn't already cover), so this does not need to cover two folds back
# to back.
_FINALIZE_TIMEOUT_S: Final = 60.0


@dataclass(frozen=True)
class CallDeps:
    """Everything the real turn machine needs, injected so the worker
    process boots (and transport tests run) with no provider keys —
    construction of the real providers is the run.py wiring's job, per
    call, behind this seam (judgment call 2)."""

    settings: Settings
    stt: SttProvider
    tts: TtsProvider
    vad_factory: Callable[[], VadModel]
    brain_factory: Callable[[str], Awaitable[BuiltBrain]]


async def _drain(stream: AsyncIterator[object]) -> None:
    """Judgment call 3: no-op consumer for an ingress fan-out in
    placeholder (deps-less) mode — without it the bounded queues would
    fill to their 30 s caps and warn-log on every frame."""
    async for _ in stream:
        pass


class CallSession:
    """One call: PeerSession + ingress/egress (+ VoiceAgentWorker when
    `deps` is provided). Satisfies signaling's `PeerSessionLike`."""

    def __init__(
        self,
        session_id: str,
        send_signal: SendSignal,
        *,
        ice_servers: tuple[IceServer, ...],
        deps: CallDeps | None = None,
        snapshot_ingestor: SnapshotIngestor | None = None,
        event_log: EventLog | None = None,
    ) -> None:
        if (snapshot_ingestor is None) != (event_log is None):
            raise ValueError("snapshot_ingestor and event_log must be provided together")
        self._session_id = session_id
        self._ingress = AudioIngress()
        self._egress = AudioEgress()
        # Judgment call 6: ContextDispatcher wiring is independent of
        # `deps` — constructed first so its `send_data_channel` closure can
        # capture `self._peer` (safe: no message reaches it before
        # `__init__` returns and `self._peer` is set).
        self._context_dispatcher: ContextDispatcher | None = None
        if snapshot_ingestor is not None and event_log is not None:
            self._context_dispatcher = ContextDispatcher(
                session_id,
                snapshot_ingestor=snapshot_ingestor,
                event_log=event_log,
                send_data_channel=self._send_context_data_channel,
            )
        self._peer = PeerSession(
            session_id,
            ingress=self._ingress,
            send_signal=send_signal,
            ice_servers=ice_servers,
            on_data=(
                self._context_dispatcher.handle_message
                if self._context_dispatcher is not None
                else None
            ),
        )
        # Judgment call 1: started lazily from handle_offer().
        self._egress_task: asyncio.Task[None] | None = None
        # Judgment call 7: close() needs to reach worker.finalize_task
        # directly, separately from waiting on worker_task itself. None
        # in deps-less (placeholder) mode, where there is no real worker.
        self._worker: VoiceAgentWorker | None = None
        tasks: list[asyncio.Task[None]] = []
        if self._context_dispatcher is not None:
            tasks.append(
                asyncio.create_task(
                    self._context_dispatcher.run(), name=f"context-dispatch:{session_id}"
                )
            )
        if deps is None:
            tasks.append(
                asyncio.create_task(
                    _drain(self._ingress.stt_stream()), name=f"stt-drain:{session_id}"
                )
            )
            tasks.append(
                asyncio.create_task(
                    _drain(self._ingress.vad_frames()), name=f"vad-drain:{session_id}"
                )
            )
        else:
            worker = VoiceAgentWorker(
                session_id,
                ingress=self._ingress,
                egress=self._egress,
                stt=deps.stt,
                tts=deps.tts,
                vad=deps.vad_factory(),
                brain_factory=partial(deps.brain_factory, session_id),
                send_message=self._peer.send_data_channel,
                settings=deps.settings,
            )
            self._worker = worker
            # Judgment call 4: the worker task starts now; its loops idle
            # on the empty fan-outs until media flows.
            worker_task = asyncio.create_task(worker.run(), name=f"worker-run:{session_id}")
            # Fail loud the moment the worker dies, not only at close():
            # a dead turn machine would otherwise be a silent no-op call.
            worker_task.add_done_callback(self._log_worker_crash)
            tasks.append(worker_task)
        self._tasks: tuple[asyncio.Task[None], ...] = tuple(tasks)

    def _send_context_data_channel(self, message: DataChannelMessage) -> None:
        self._peer.send_data_channel(message)

    def _log_worker_crash(self, task: asyncio.Task[None]) -> None:
        if task.cancelled() or task.exception() is None:
            return
        log.error(
            "voice_worker.worker_task_crashed",
            session_id=self._session_id,
            task=task.get_name(),
            error=repr(task.exception()),
        )

    async def handle_offer(self, sdp: str) -> None:
        await self._peer.handle_offer(sdp)
        if self._egress_task is None:  # not a re-offer (ICE restart) — start once
            self._egress_task = asyncio.create_task(
                self._egress.run(self._peer.outbound_sink),
                name=f"egress-run:{self._session_id}",
            )

    async def handle_remote_ice(self, payload: object) -> None:
        await self._peer.handle_remote_ice(payload)  # type: ignore[arg-type]

    async def close(self) -> None:
        """Judgment call 5: peer → egress stop → bounded wait → loud
        cancel. `_egress_task` may be None if the connection closed
        before any offer arrived. Judgment call 7: THEN, separately,
        wait for the worker's post-call finalize to genuinely finish —
        see that judgment call for why this method does not return, and
        `_active[session_id]` does not free, until it has (or has been
        given every reasonable chance to)."""
        await self._peer.close()
        self._egress.stop()
        if self._context_dispatcher is not None:
            # ContextDispatcher judgment call 7 (its own, unrelated
            # numbering — context_dispatch.py's docstring): sentinel, not
            # cancellation — the channel is already shut, so run() drains
            # promptly instead of the bounded wait below always paying
            # the full _CLOSE_TIMEOUT_S on a task that never ends on its
            # own.
            self._context_dispatcher.close()
        tasks = self._tasks if self._egress_task is None else (*self._tasks, self._egress_task)
        done, pending = await asyncio.wait(tasks, timeout=_CLOSE_TIMEOUT_S)
        for task in pending:
            log.warning("voice_worker.call_task_cancelled", task=task.get_name())
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        # Fail loud on unexpected task crashes swallowed by the gather —
        # a worker task that died with a real exception must be visible.
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                log.error(
                    "voice_worker.call_task_failed",
                    task=task.get_name(),
                    error=repr(task.exception()),
                )
        await self._await_finalize()

    async def _await_finalize(self) -> None:
        """Judgment call 7 (CRITICAL fix, 2026-08-07). By the time this
        runs, `worker_task` — one of `tasks` above — is guaranteed to be
        in `done` or already `gather()`-ed to completion, and `run()`'s
        finally always runs on the way there (finally blocks run under
        cancellation too), so `self._worker.finalize_task` is reliably
        set here whenever `self._worker` is not None. What it is NOT
        guaranteed to be is FINISHED — worker.py shields it from exactly
        the cancellation `close()` may have just delivered above.

        `asyncio.wait(..., timeout=...)`, not `wait_for` — `wait_for`
        cancels its argument on timeout, which is precisely the bug this
        whole fix removes; `wait()` only stops WAITING, it never touches
        the task. If `_FINALIZE_TIMEOUT_S` elapses, the finalize task is
        still left running to its own real completion in the background,
        and the done-callback worker.py already attached
        (`_log_finalize_crash`) is what still logs a genuine late
        failure — this method's own log line below is only for "still
        running, close() is giving up waiting", not for the task's
        eventual outcome."""
        task = self._worker.finalize_task if self._worker is not None else None
        if task is None:
            return
        done, pending = await asyncio.wait({task}, timeout=_FINALIZE_TIMEOUT_S)
        if pending:
            log.error(
                "voice_worker.finalize_still_pending_after_close",
                session_id=self._session_id,
                timeout_s=_FINALIZE_TIMEOUT_S,
            )
            return
        (finished,) = done
        if not finished.cancelled() and finished.exception() is not None:
            # Already logged once by worker.py's own `except Exception`
            # branch inside run() when this ISN'T the cancelled-outer-
            # task case — this covers the other path: `finalize_task`
            # failing on its own terms, with nothing else ever having
            # awaited it directly. `_log_finalize_crash`'s done-callback
            # also logs this same exception; a duplicate log line here
            # is preferable to a failed finalize being invisible from
            # close()'s own perspective.
            log.error(
                "voice_worker.finalize_failed",
                session_id=self._session_id,
                error=repr(finished.exception()),
            )


__all__ = ["CallDeps", "CallSession"]
