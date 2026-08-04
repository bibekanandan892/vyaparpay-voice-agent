"""`SnapshotIngestor` (docs/08-context-and-events.md §4.1) — validates and
stores inbound `screen_context/v1` snapshots and `ctx.delta` payloads.

docs/08 §4's preamble: "`SnapshotIngestor` runs in agent-api for the REST
snapshot and in voice-worker for data-channel traffic — same class, same
validation, two entrypoints." This class has *three* public ingest methods
because the REST entrypoint (`POST /v1/sessions`'s `screen_context` field,
`protocol/schemas/session_create_request.v1.json`) carries the bare IR with
no envelope/seq at all, while the two in-call entrypoints carry the full
`{v, type, seq, ts, payload}` envelope (`protocol/schemas/
data_channel_envelope.v1.json`):

- `ingest_initial_snapshot` — the REST path.
- `ingest_snapshot` / `ingest_delta` — the data-channel paths.

Every method runs docs/08 §4.1's four checks, in the documented cheapest-
first order, and returns an `IngestResult` rather than raising: an invalid
message is an *expected*, handled outcome ("the pipeline degrades context,
never conversation", docs/08 §7's closing line), not a programming error.

Judgment calls, flagged per house style:

1. **Sequence continuity is checked against the last snapshot/delta
   *this component itself* ingested — not a channel-wide counter spanning
   interleaved `ctx.event` messages.** docs/08 §3.2 frames the client's
   `seq` as one counter shared across all three `ctx.*` types "so the
   backend runs exactly one gap detector instead of three", and taken
   completely literally, a strict `seq == last + 1` check would need
   visibility into every event's `seq` too — visibility this component,
   scoped to snapshot/delta ingestion, does not have (docs/08 §4.2 gives
   `EventLog` no such reporting duty, and this task's own component split
   is "validate → store → shape", not "validate everything in one place").
   Proof this matters, not just a convenience: docs/08 §6's own worked
   sequence trace has `ctx.event {"seq":16}` immediately followed by
   `ctx.delta {"seq":17, "base_seq":15}` — a literal `seq == last_delta_seq
   + 1` check (15+1=16) would reject that exact canonical delta as a false
   gap. What actually keeps a delta safe is `base_seq` — `ctx_delta.v1
   .json`'s own description states it is deliberately "NOT simply the
   previous message's seq" for exactly this reason, and docs/08 §4.1's
   closing sentence names it as the thing that "makes the merge refuse to
   apply a diff against a state the client did not diff against." This
   class therefore checks, per type: a **snapshot** needs `seq` strictly
   greater than the last-stored seq (freshness, not adjacency — a
   gap-recovery snapshot's `seq` is explicitly *not* required to be
   adjacent, docs/08 §3.3 step 3); a **delta** needs both `seq` strictly
   greater than the last-stored seq AND `base_seq == last-stored seq` (the
   correctness-critical check). True channel-wide gap detection spanning
   lost *events* (a seq jump with no snapshot/delta ever seen in between)
   needs the data-channel dispatcher itself — the component that sees
   every `ctx.*` message before routing it — to also feed event `seq`
   values through a shared tracker; that dispatcher doesn't exist as code
   yet (T3/voice-worker scope), so this is flagged here as an integration
   note rather than silently guessed at.
2. **Oversized deltas are dropped, not recompressed.** See
   `context_compressor.py`'s module docstring for the full reasoning: the
   docs/07 §7 drop ladder is IR-shaped and cannot be safely applied to a
   delta's `changed`/`removed` arrays without risking a corrupted merge.
3. **Post-merge state is re-validated against the full
   `screen_context.v1.json` schema before being written back — this is the
   judgment call the task brief flags explicitly.** `ctx_delta.v1.json`'s
   own description names the gap: its `changed_component` def validates
   only `role` + `label` at the schema level (deliberately, per the T1
   freeze, to avoid duplicating the sixteen-role discriminated union a
   second time) — so e.g. a `dialog` entry missing `visible` passes check
   (c) but produces a semantically incomplete merged IR. Resolution: after
   computing the merge, validate the *result* against
   `screen_context.v1.json` (the schema authority for a complete IR) before
   writing anything to Redis. A failure here is treated exactly like a
   check-(c) schema failure (`REJECTED_SCHEMA`, `needs_snapshot_request=
   True`) rather than writing the incomplete result or trying to patch it:
   docs/08 §7's failure-mode table is explicit that a malformed snapshot
   must be "reject[ed] wholesale, never partially ingest[ed]" precisely
   because "the risk is the *unhandled* path: garbage entering the
   prompt" — a delta that merges into something incomplete is exactly that
   risk, just detected one step later than a delta whose own shape is
   already wrong. The previously-stored (known-good) state is left
   untouched; the client is asked for a fresh full snapshot rather than
   continuing to layer more deltas onto a state this component now
   distrusts.
4. **The REST entrypoint seeds `seq=0` as the session's genesis anchor.**
   `session_create_request.v1.json`'s `screen_context` field carries no
   `seq` of its own, but a `ctx.delta` arriving in-call still needs
   *something* to verify its first `base_seq` against. Seeding `0` means a
   first delta must carry `base_seq: 0` to be accepted — this is an
   assumption about client seq-numbering (that the data-channel counter's
   first value that could be "diffed against" is 0, or that the client's
   first message is always a fresh `ctx.snapshot` rather than a `ctx.delta`
   against the REST-ingested state) that no doc pins explicitly; flagged
   here as a question for the on-device publisher spec (docs/07), not
   something this component can resolve unilaterally.
5. **"Count metric" (docs/08 §4.1's check-(a) failure action) is a
   structured log line, not a dedicated counter.** No metrics/counter
   module exists anywhere in `app/` yet (`app/obs/` only wires
   logging/tracing) — every comparable "this failed, record it" path in
   the codebase today (e.g. `app/agent/context_builder.py`'s
   `context.user_profile.merchant_missing`) uses `log.warning(...)` with a
   stable event name as the aggregable signal. This module follows the
   same convention rather than introducing new observability
   infrastructure this task was not asked to build.
"""

from __future__ import annotations

from typing import Any, Final

import orjson

from app.context.context_compressor import ContextCompressor
from app.context.models import IngestOutcome, IngestResult
from app.context.redis_keys import CTX_TTL_SECONDS, _ctx_key
from app.context.schema_validation import load_schema, validate
from app.data.redis_client import RedisClient
from app.obs.logging import get_logger

log = get_logger(__name__)

# docs/08 §4.1: "Serialized payload ≤ 8 KiB (a rung-compliant snapshot is
# ~1.2 KiB; the cap is client-bug detection, not tuning)."
SIZE_CAP_BYTES: Final = 8 * 1024

_SNAPSHOT_ENVELOPE_TYPE: Final = "ctx.snapshot"
_DELTA_ENVELOPE_TYPE: Final = "ctx.delta"
_SCREEN_CONTEXT_VERSION: Final = "screen_context/v1"

# Judgment call 4 above.
_GENESIS_SEQ: Final = 0

_DELTA_MERGE_FIELDS: Final = ("last_action", "last_api", "loading", "dirty_fields")


def _is_plain_int(value: Any) -> bool:
    """`bool` is an `int` subclass in Python but not a JSON integer for our
    purposes — `True`/`False` must never satisfy a `seq`/`ts` check."""
    return isinstance(value, int) and not isinstance(value, bool)


def _check_envelope_shape(envelope: dict[str, Any], *, expected_type: str) -> str | None:
    """Check (a), docs/08 §4.1: `v == 1`, known `type`, integer `seq`,
    epoch-ms `ts`. Cheap and hand-written on purpose — the more expensive
    full schema walk is check (c)."""
    v = envelope.get("v")
    if not _is_plain_int(v) or v != 1:
        return f"envelope.v must be the integer 1 (got {v!r})"
    if envelope.get("type") != expected_type:
        return f"envelope.type must be {expected_type!r} (got {envelope.get('type')!r})"

    seq = envelope.get("seq")
    # Split into separate `isinstance` checks (rather than routed through
    # `_is_plain_int`) so mypy can actually narrow `seq`/`ts` to `int` for
    # the `< 0` comparison below — a custom bool-returning helper gives it
    # no such narrowing, `isinstance` does.
    if not isinstance(seq, int):
        return f"envelope.seq must be a non-negative integer (got {seq!r})"
    if isinstance(seq, bool) or seq < 0:
        return f"envelope.seq must be a non-negative integer (got {seq!r})"

    ts = envelope.get("ts")
    if not isinstance(ts, int):
        return f"envelope.ts must be a non-negative epoch-ms integer (got {ts!r})"
    if isinstance(ts, bool) or ts < 0:
        return f"envelope.ts must be a non-negative epoch-ms integer (got {ts!r})"

    if not isinstance(envelope.get("payload"), dict):
        return "envelope.payload must be an object"
    return None


def _size_bytes(payload: dict[str, Any]) -> int:
    return len(orjson.dumps(payload))


def _identity(component: dict[str, Any]) -> tuple[Any, Any]:
    """docs/07 §6: "Component identity for diffing is testTag when
    present, else `role` + `label`" — testTag never appears on the wire in
    this protocol (screen_context.v1.json's own top-level description), so
    role+label is always the identity in practice."""
    return (component.get("role"), component.get("label"))


def _apply_delta(ir: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    """docs/07 §6's merge algorithm: `removed` entries drop by identity
    first (so a `removed` + `changed` pair sharing an identity — the "label
    changed" remove-then-add case docs/07 §6 explicitly accepts — lands as
    a fresh append, not a stale in-place replace); `changed` entries then
    replace an identity-matching component in place or append when new;
    top-level fields copy over only when the delta actually carries them."""
    components: list[dict[str, Any] | None] = list(ir.get("components", []))
    index_by_identity = {_identity(c): i for i, c in enumerate(components) if c is not None}

    for removed in delta.get("removed", []):
        key = (removed.get("role"), removed.get("label"))
        idx = index_by_identity.pop(key, None)
        if idx is not None:
            components[idx] = None

    for changed in delta.get("changed", []):
        key = _identity(changed)
        idx = index_by_identity.get(key)
        if idx is not None:
            components[idx] = changed
        else:
            components.append(changed)
            index_by_identity[key] = len(components) - 1

    merged: dict[str, Any] = {**ir, "components": [c for c in components if c is not None]}
    for field in _DELTA_MERGE_FIELDS:
        if field in delta:
            merged[field] = delta[field]
    return merged


class SnapshotIngestor:
    """`compressor` is a required dependency, not optional: check (b)'s
    documented failure action for an oversized *snapshot* is "route to
    `ContextCompressor` re-compression" (docs/08 §4.1), and
    `context_compressor.py`'s own docstring frames the drop ladder as "one
    drop-ladder definition, two enforcement points" — this and
    `ContextCompressor.render_snapshot_slot` are those two points."""

    def __init__(self, redis: RedisClient, compressor: ContextCompressor) -> None:
        self._redis = redis
        self._compressor = compressor

    # ------------------------------------------------------------------
    # REST entrypoint — POST /v1/sessions' screen_context field
    # ------------------------------------------------------------------

    async def ingest_initial_snapshot(
        self, session_id: str, screen_context: dict[str, Any], *, received_at_ms: int
    ) -> IngestResult:
        """No envelope, no `seq` (see module docstring judgment calls 4).
        Checks (a)/(d) degrade to their REST-appropriate minimum: (a) is
        just the version-marker check `sessions.py`'s
        `_require_supported_screen_context` stub already does a partial
        version of (that function's own docstring names this class as its
        eventual full replacement); (d) is trivially satisfied — a brand
        new session has no prior state to be continuous with."""
        if screen_context.get("v") != _SCREEN_CONTEXT_VERSION:
            reason = f"screen_context.v must be {_SCREEN_CONTEXT_VERSION!r}"
            log.warning(
                "context.snapshot_ingestor.envelope_rejected",
                session_id=session_id,
                reason=reason,
            )
            return IngestResult(
                session_id=session_id,
                outcome=IngestOutcome.REJECTED_ENVELOPE,
                accepted=False,
                reason=reason,
            )

        payload, dropped_rungs, recompressed = self._enforce_size_cap_snapshot(
            session_id, screen_context
        )

        schema_errors = validate(payload, load_schema("screen_context.v1"))
        if schema_errors:
            return self._reject_schema(session_id, schema_errors)

        stored = {**payload, "seq": _GENESIS_SEQ, "received_ts": received_at_ms}
        await self._write_ctx(session_id, stored)
        return IngestResult(
            session_id=session_id,
            outcome=(
                IngestOutcome.ACCEPTED_AFTER_RECOMPRESSION
                if recompressed
                else IngestOutcome.ACCEPTED
            ),
            accepted=True,
            seq=_GENESIS_SEQ,
            dropped_rungs=dropped_rungs,
        )

    # ------------------------------------------------------------------
    # Data-channel entrypoints — ctx.snapshot / ctx.delta
    # ------------------------------------------------------------------

    async def ingest_snapshot(self, session_id: str, envelope: dict[str, Any]) -> IngestResult:
        envelope_error = _check_envelope_shape(envelope, expected_type=_SNAPSHOT_ENVELOPE_TYPE)
        if envelope_error is not None:
            return self._reject_envelope(session_id, envelope_error)

        seq = envelope["seq"]
        stored_before = await self._read_ctx(session_id)
        last_seq = stored_before.get("seq") if stored_before is not None else None
        if last_seq is not None and seq <= last_seq:
            return self._reject_sequence_gap(
                session_id, f"snapshot seq {seq} is not newer than last-stored seq {last_seq}"
            )

        payload, dropped_rungs, recompressed = self._enforce_size_cap_snapshot(
            session_id, envelope["payload"]
        )

        schema_errors = validate(payload, load_schema("screen_context.v1"))
        if schema_errors:
            return self._reject_schema(session_id, schema_errors)

        stored = {**payload, "seq": seq, "received_ts": envelope["ts"]}
        await self._write_ctx(session_id, stored)
        return IngestResult(
            session_id=session_id,
            outcome=(
                IngestOutcome.ACCEPTED_AFTER_RECOMPRESSION
                if recompressed
                else IngestOutcome.ACCEPTED
            ),
            accepted=True,
            seq=seq,
            dropped_rungs=dropped_rungs,
        )

    async def ingest_delta(self, session_id: str, envelope: dict[str, Any]) -> IngestResult:
        envelope_error = _check_envelope_shape(envelope, expected_type=_DELTA_ENVELOPE_TYPE)
        if envelope_error is not None:
            return self._reject_envelope(session_id, envelope_error)

        payload = envelope["payload"]
        if _size_bytes(payload) > SIZE_CAP_BYTES:
            # Judgment call 2: oversized deltas are dropped, not recompressed.
            return self._reject_sequence_gap(
                session_id,
                f"delta payload {_size_bytes(payload)}B exceeds the {SIZE_CAP_BYTES}B cap "
                "and cannot be safely recompressed (see context_compressor.py)",
                outcome=IngestOutcome.REJECTED_OVERSIZE_DELTA,
            )

        schema_errors = validate(payload, load_schema("ctx_delta.v1"))
        if schema_errors:
            return self._reject_schema(session_id, schema_errors)

        seq = envelope["seq"]
        stored_before = await self._read_ctx(session_id)
        last_seq = stored_before.get("seq") if stored_before is not None else None
        base_seq = payload["base_seq"]
        if stored_before is None or seq <= last_seq or base_seq != last_seq:
            return self._reject_sequence_gap(
                session_id,
                f"delta base_seq={base_seq} seq={seq} does not continue from last-stored "
                f"seq={last_seq}",
            )

        merged = _apply_delta(stored_before, payload)
        # Judgment call 3: re-validate the merge PRODUCT, not just the delta.
        post_merge_errors = validate(merged, load_schema("screen_context.v1"))
        if post_merge_errors:
            return self._reject_schema(
                session_id, post_merge_errors, detail="post-merge state failed full IR validation"
            )

        stored = {**merged, "seq": seq, "received_ts": envelope["ts"]}
        await self._write_ctx(session_id, stored)
        return IngestResult(
            session_id=session_id, outcome=IngestOutcome.ACCEPTED, accepted=True, seq=seq
        )

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _enforce_size_cap_snapshot(
        self, session_id: str, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], tuple[int, ...], bool]:
        """Check (b) for a snapshot/initial-IR payload: within cap ->
        unchanged; over cap -> routed through `ContextCompressor`'s drop
        ladder (docs/08 §4.1). Returns `(payload, dropped_rungs,
        recompressed)`."""
        if _size_bytes(payload) <= SIZE_CAP_BYTES:
            return payload, (), False
        log.warning(
            "context.snapshot_ingestor.oversize_snapshot",
            session_id=session_id,
            size_bytes=_size_bytes(payload),
        )
        result = self._compressor.recompress_oversize(payload)
        return result.ir, result.dropped_rungs, True

    async def _read_ctx(self, session_id: str) -> dict[str, Any] | None:
        raw = await self._redis.raw.get(_ctx_key(session_id))
        return orjson.loads(raw) if raw else None

    async def _write_ctx(self, session_id: str, stored: dict[str, Any]) -> None:
        await self._redis.raw.set(
            _ctx_key(session_id), orjson.dumps(stored).decode(), ex=CTX_TTL_SECONDS
        )

    def _reject_envelope(self, session_id: str, reason: str) -> IngestResult:
        log.warning(
            "context.snapshot_ingestor.envelope_rejected", session_id=session_id, reason=reason
        )
        return IngestResult(
            session_id=session_id,
            outcome=IngestOutcome.REJECTED_ENVELOPE,
            accepted=False,
            reason=reason,
        )

    def _reject_schema(
        self, session_id: str, errors: list[str], *, detail: str = "schema validation failed"
    ) -> IngestResult:
        log.warning(
            "context.snapshot_ingestor.schema_rejected",
            session_id=session_id,
            detail=detail,
            error_count=len(errors),
        )
        return IngestResult(
            session_id=session_id,
            outcome=IngestOutcome.REJECTED_SCHEMA,
            accepted=False,
            needs_snapshot_request=True,
            reason=detail,
        )

    def _reject_sequence_gap(
        self,
        session_id: str,
        reason: str,
        *,
        outcome: IngestOutcome = IngestOutcome.REJECTED_SEQUENCE_GAP,
    ) -> IngestResult:
        log.warning(
            "context.snapshot_ingestor.sequence_gap", session_id=session_id, reason=reason
        )
        return IngestResult(
            session_id=session_id,
            outcome=outcome,
            accepted=False,
            needs_snapshot_request=True,
            reason=reason,
        )


__all__ = ["SIZE_CAP_BYTES", "SnapshotIngestor"]
