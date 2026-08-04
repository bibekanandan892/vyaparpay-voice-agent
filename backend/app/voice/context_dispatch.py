"""ContextDispatcher — Phase-4 T4's wiring of inbound `ctx.*` data-channel
traffic into the Phase-4 T2 context-ingestion pipeline
(`app/context/{SnapshotIngestor,EventLog}`), plus the `ctx.request_snapshot`
server->client recovery message (docs/13-api-contracts.md §4, docs/08
-context-and-events.md §3).

This is the `on_data` seam `PeerSession`'s own docstring (judgment call 7)
reserves for exactly this task: "the `on_data` constructor seam exists so
that task plugs in a parser without touching this file." `peer_session.py`
is not modified — this module is the parser that plugs into it.

Judgment calls, flagged per house style:

1. **Inbound messages are processed by ONE serialized consumer loop, not a
   fire-and-forget `asyncio.create_task()` per message.** The `ctx` channel
   is reliable and ordered (docs/13 §4), and `SnapshotIngestor.ingest_delta`
   is correctness-critical on that ordering: it read-modify-writes
   `ctx:{session_id}` (`base_seq`-verified merge). If two deltas arriving
   back-to-back were each dispatched as an independent task, both could
   read the SAME pre-write state before either writes back (task 1 hits its
   first `await` on the Redis read, the loop switches to task 2, which
   reads the identical stale state) — a real, not theoretical, false-gap
   race. Routing every inbound message through one `asyncio.Queue` drained
   by a single `run()` task removes the ambiguity entirely: processing
   order is exactly delivery order, matching what the channel already
   guarantees, no reliance on asyncio's internal task-scheduling fairness.
2. **Exception-safety at two boundaries.** `handle_message()` — the actual
   `OnData` callback `PeerSession._on_channel_message` invokes directly,
   with no try/except of its own — only ever does a `put_nowait()`, wrapped
   in its own broad `except Exception` for defense in depth. The real
   parsing/dispatch work happens inside `run()`'s loop, one `try/except`
   per message, so one malformed or pathological message degrades context
   for that message only and never stops the loop or raises back through
   aiortc's synchronous event-emitter dispatch (which has no handler of its
   own to catch it) — the task's own framing: "a data-channel callback
   raising could plausibly kill something upstream you don't want it to."
3. **`last_good_seq` (the `ctx.request_snapshot` payload's one field,
   `protocol/schemas/data_channel_envelope.v1.json`: "Last client seq the
   server ingested cleanly") is tracked locally by this dispatcher, not
   read back out of `app/context/`.** `SnapshotIngestor`'s `IngestResult`
   (`app/context/models.py`) does not expose the last-stored seq on a
   rejection — by design, `reason` is "a short, fixed diagnostic string...
   never raw payload content", not a machine-readable field — and this
   task's non-goals forbid modifying `app/context/**` to add one. Reaching
   into `SnapshotIngestor`'s private Redis read (`_read_ctx`/`_ctx_key`)
   to recompute it would exactly contradict `redis_keys.py`'s own stated
   philosophy ("importing a private, underscore-prefixed helper across
   module boundaries would couple this package to X's internals").
   Instead: this dispatcher is the ONLY caller of `ingest_snapshot`/
   `ingest_delta` for its session (docs/08 §4.1: "one call pins one worker"
   — the demo's single-writer property is structural), so it can track
   "the last accepted `seq`" itself from every `IngestResult.accepted`
   response, which is provably equivalent to a fresh Redis read of
   `ctx:{session_id}`'s stored `seq` at all times. Starts at `0`, matching
   `SnapshotIngestor`'s own REST-entrypoint genesis anchor (`_GENESIS_SEQ`)
   for a session whose only prior state is the `POST /v1/sessions` snapshot
   this dispatcher never itself observed.
4. **`ctx.request_snapshot` gets its own `seq` counter, separate from
   `VoiceAgentWorker._seq`.** docs/13 §4 frames this at direction
   granularity ("Each direction runs its own `seq` counter"), and docs/08
   §3.2 is explicit that the server stream's counter belongs to
   `ctx.request_snapshot` itself ("Server messages use the envelope with
   their own counter, which the client does not gap-check. The only
   server-originated type is `ctx.request_snapshot`"). Sharing
   `VoiceAgentWorker`'s counter is not mechanically available without
   changing that class's frozen, heavily-reviewed contract: `_seq` is a
   private per-instance counter with no accessor, and `VoiceAgentWorker`
   does not exist at all in placeholder/deps-less `CallSession` mode
   (`call_session.py`), while this dispatcher is wired unconditionally
   (judgment call 6) — there is no single worker instance to share a
   counter WITH in that mode. Since docs/13 §4 and `DataChannelMessage`'s
   own docstring both say the client "never gap-checks the server stream"
   (a lost `ctx.request_snapshot` "costs nothing, because the next client
   message with a gapped seq re-triggers it", docs/08 §3.2), this is a
   protocol-cleanliness question, not a correctness one — a second,
   independent, gapless-within-itself counter is the lower-coupling choice
   given no test or client behavior depends on cross-type interleaving.
5. **Schema validation is asymmetric by design, matching each T2 component's
   own documented division of labor.** `ctx.snapshot`/`ctx.delta` are
   passed to `SnapshotIngestor` untouched — it already runs the full
   docs/08 §4.1 four-check pipeline (envelope shape, size cap, schema,
   sequence continuity) and this dispatcher would only be duplicating it.
   `ctx.event` gets NO such treatment from `EventLog.append` by that
   module's own explicit design ("Deliberately no validation here... A
   caller (T3's data-channel dispatcher) that wants validated events can
   run them through `app.context.schema_validation.validate(event,
   ...load_schema('app_event.v1'))` itself before calling `append`" —
   `event_log.py`'s module docstring, naming this exact task even if by an
   earlier task label). This dispatcher does exactly that: validates the
   envelope's `payload` against `app_event.v1` and drops (log + ignore, no
   `ctx.request_snapshot` — gap-recovery is a snapshot/delta concept only)
   on failure, never calling `append` with unvalidated content.
6. **Wiring does not depend on `CallDeps`.** `call_session.py`'s
   `snapshot_ingestor`/`event_log` constructor parameters are independent
   of `deps` (which gates the voice turn machine on provider keys) —
   context capture has nothing to do with STT/TTS/brain availability, so a
   call with no voice keys configured (placeholder mode) still gets ctx.*
   ingestion if the process was given a `SnapshotIngestor`/`EventLog` pair.
   Both must be supplied together or neither — a partial pair is treated as
   a caller bug and raises immediately rather than silently degrading.
7. **`run()` needs an explicit `close()` sentinel — it cannot end on its
   own the way the ingress fan-out drains in `call_session.py` do.** Those
   drains end naturally when `AudioIngress.close()` ends their async
   iterator (part of `PeerSession.close()`'s own teardown); this
   dispatcher's queue has no such upstream "no more messages" signal —
   without one, `CallSession.close()`'s bounded `asyncio.wait(self._tasks,
   timeout=_CLOSE_TIMEOUT_S)` would ALWAYS burn the full
   `_CLOSE_TIMEOUT_S` (5 s) on this task alone, on every call, since it can
   never be in `done` before the timeout. `close()` pushes a `None`
   sentinel (same shape as `peer_session.py`'s `_EgressTrack.end()`) so
   `run()` drains whatever is already queued, then returns promptly —
   `CallSession.close()` calls it right after `self._peer.close()` (the
   channel is shut by then, so no new message can race the sentinel).
8. **Channel-wide gap detection spanning `ctx.event` is explicitly NOT
   implemented here.** docs/08 §3.2's "one `seq` counter across all three
   client types" means a gap could, in principle, be an event that never
   arrived with no snapshot/delta involved at all — `SnapshotIngestor`'s
   own module docstring (judgment call 1) already flags this as needing
   "the data-channel dispatcher itself... to also feed event `seq` values
   through a shared tracker" and explicitly leaves it as "an integration
   note rather than silently guessed at" for whichever task builds the
   dispatcher. This task's brief scopes the dispatcher to wiring the
   `needs_snapshot_request` signal `SnapshotIngestor` already computes, not
   building a new cross-type gap detector; inventing one un-asked-for would
   risk exactly the kind of behavior no fixture or doc worked example pins
   down. Flagged here, the same way T2 flagged it, rather than silently
   dropped.

Docs: docs/13 §4 (wire shapes, seq semantics), docs/08 §3 (transport,
gap detection), docs/08 §4 (SnapshotIngestor/EventLog).
Tests: tests/voice/test_context_dispatch.py.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any, Final

import orjson

from app.context.event_log import EventLog
from app.context.models import IngestResult
from app.context.schema_validation import load_schema, validate
from app.context.snapshot_ingestor import SnapshotIngestor
from app.domain.voice import DataChannelMessage
from app.obs.logging import get_logger

log = get_logger(__name__)

# The client->server ctx.* types this dispatcher routes (docs/13 §4). Local
# constants, not domain/voice.py's DC_TYPE_* — that file's own comment
# assigns ctx.* naming to protocol/(docs/08), the same convention
# snapshot_ingestor.py already follows for its own _SNAPSHOT_ENVELOPE_TYPE/
# _DELTA_ENVELOPE_TYPE constants.
CTX_TYPE_SNAPSHOT: Final = "ctx.snapshot"
CTX_TYPE_DELTA: Final = "ctx.delta"
CTX_TYPE_EVENT: Final = "ctx.event"

# The one server->client type this module emits (docs/13 §4/docs/08 §3.3).
DC_TYPE_CTX_REQUEST_SNAPSHOT: Final = "ctx.request_snapshot"

# Judgment call 3: matches snapshot_ingestor.py's own _GENESIS_SEQ.
_GENESIS_LAST_GOOD_SEQ: Final = 0

SendDataChannel = Callable[[DataChannelMessage], None]


def _now_epoch_ms() -> int:
    return int(time.time() * 1000)


class ContextDispatcher:
    """One instance per call, constructed in `call_session.py` alongside
    `PeerSession`. `handle_message` is the `OnData` callback; `run()` is an
    externally-owned task (created by `CallSession`, exactly like
    `VoiceAgentWorker.run()`) that drains the queue until `close()`'s
    sentinel (judgment call 7) or cancellation."""

    def __init__(
        self,
        session_id: str,
        *,
        snapshot_ingestor: SnapshotIngestor,
        event_log: EventLog,
        send_data_channel: SendDataChannel,
        now_ms: Callable[[], int] = _now_epoch_ms,
    ) -> None:
        self._session_id = session_id
        self._snapshot_ingestor = snapshot_ingestor
        self._event_log = event_log
        self._send_data_channel = send_data_channel
        self._now_ms = now_ms
        # `None` is the judgment-call-7 shutdown sentinel, never a real
        # inbound message (aiortc's OnData type is `str | bytes`).
        self._queue: asyncio.Queue[str | bytes | None] = asyncio.Queue()
        self._seq = 0
        # Judgment call 3.
        self._last_good_seq = _GENESIS_LAST_GOOD_SEQ

    # ------------------------------------------------------------------
    # OnData seam (peer_session.py's PeerSession(..., on_data=...))
    # ------------------------------------------------------------------

    def handle_message(self, message: str | bytes) -> None:
        """Sync, never raises (judgment call 2): enqueues for `run()`'s
        serialized consumer and returns immediately."""
        try:
            self._queue.put_nowait(message)
        except Exception:
            log.warning(
                "context_dispatch.enqueue_failed", session_id=self._session_id, exc_info=True
            )

    def close(self) -> None:
        """Judgment call 7: sentinel so `run()` drains whatever is already
        queued and returns promptly instead of needing cancellation.
        `CallSession.close()` calls this right after `self._peer.close()`.
        Idempotent-enough to call more than once (each call just queues
        another sentinel; `run()` returns on the first one it sees)."""
        self._queue.put_nowait(None)

    # ------------------------------------------------------------------
    # The consumer loop — CallSession creates/cancels this task.
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Drain the queue until `close()`'s sentinel or cancellation
        (judgment call 1: exactly one consumer, so processing order is
        delivery order)."""
        while True:
            message = await self._queue.get()
            if message is None:  # judgment call 7
                return
            try:
                await self._process(message)
            except Exception:
                # Judgment call 2: one bad message degrades context for
                # itself only — the loop, and the call, keep running.
                log.warning(
                    "context_dispatch.processing_failed",
                    session_id=self._session_id,
                    exc_info=True,
                )

    async def _process(self, message: str | bytes) -> None:
        envelope = self._parse_envelope(message)
        if envelope is None:
            return
        type_ = envelope["type"]
        if type_ == CTX_TYPE_SNAPSHOT:
            result = await self._snapshot_ingestor.ingest_snapshot(self._session_id, envelope)
            self._after_ingest(result)
        elif type_ == CTX_TYPE_DELTA:
            result = await self._snapshot_ingestor.ingest_delta(self._session_id, envelope)
            self._after_ingest(result)
        elif type_ == CTX_TYPE_EVENT:
            await self._handle_event(envelope)
        else:
            # docs/13 §9: unknown types are additive-within-v1 and MUST be
            # ignored, never treated as an error — matches
            # peer_session.py's own log.debug-level "ignored" convention.
            log.debug(
                "context_dispatch.unknown_type_ignored", session_id=self._session_id, type=type_
            )

    def _parse_envelope(self, message: str | bytes) -> dict[str, Any] | None:
        try:
            decoded = orjson.loads(message)
        except orjson.JSONDecodeError:
            log.warning("context_dispatch.malformed_json", session_id=self._session_id)
            return None
        if not isinstance(decoded, dict):
            log.warning(
                "context_dispatch.envelope_not_object",
                session_id=self._session_id,
                decoded_type=type(decoded).__name__,
            )
            return None
        if not isinstance(decoded.get("type"), str):
            log.warning("context_dispatch.missing_type", session_id=self._session_id)
            return None
        return decoded

    # ------------------------------------------------------------------
    # ctx.event (judgment call 5)
    # ------------------------------------------------------------------

    async def _handle_event(self, envelope: dict[str, Any]) -> None:
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            log.warning("context_dispatch.event_payload_not_object", session_id=self._session_id)
            return
        errors = validate(payload, load_schema("app_event.v1"))
        if errors:
            log.warning(
                "context_dispatch.event_schema_rejected",
                session_id=self._session_id,
                error_count=len(errors),
            )
            return
        await self._event_log.append(self._session_id, payload)

    # ------------------------------------------------------------------
    # ctx.snapshot / ctx.delta outcome -> ctx.request_snapshot
    # ------------------------------------------------------------------

    def _after_ingest(self, result: IngestResult) -> None:
        if result.accepted:
            if result.seq is not None:
                # Judgment call 3: the local last-known-good tracker.
                self._last_good_seq = result.seq
            return
        if result.needs_snapshot_request:
            self._send_request_snapshot()

    def _send_request_snapshot(self) -> None:
        # Judgment call 4: this dispatcher's own counter.
        self._seq += 1
        message = DataChannelMessage(
            type=DC_TYPE_CTX_REQUEST_SNAPSHOT,
            seq=self._seq,
            ts=self._now_ms(),
            payload={"last_good_seq": self._last_good_seq},
        )
        try:
            self._send_data_channel(message)
        except Exception:
            # Same best-effort convention as worker.py's _send_dc (judgment
            # call 8 there): degrade loudly, never fatally.
            log.warning(
                "context_dispatch.request_snapshot_send_failed",
                session_id=self._session_id,
                seq=message.seq,
                exc_info=True,
            )


__all__ = [
    "CTX_TYPE_DELTA",
    "CTX_TYPE_EVENT",
    "CTX_TYPE_SNAPSHOT",
    "DC_TYPE_CTX_REQUEST_SNAPSHOT",
    "ContextDispatcher",
    "SendDataChannel",
]
