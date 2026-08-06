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
   `_CLOSE_TIMEOUT_S` to finish before the cancel.
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
        before any offer arrived."""
        await self._peer.close()
        self._egress.stop()
        if self._context_dispatcher is not None:
            # ContextDispatcher judgment call 7: sentinel, not cancellation
            # — the channel is already shut, so run() drains promptly
            # instead of the bounded wait below always paying the full
            # _CLOSE_TIMEOUT_S on a task that never ends on its own.
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


__all__ = ["CallDeps", "CallSession"]
