"""Domain value types local to `app/context/` (docs/08-context-and-events.md
§4). Per this task's brief: the frozen Batch-1 contract types in
`app/domain/types.py` / `app/domain/interfaces.py` have no shape for what
`SnapshotIngestor`/`ContextCompressor` return, and this task does not edit
those frozen files — new types belong here instead, colocated with the
components that produce them.

Frozen pydantic models throughout, matching `app/domain/types.py`'s own
`_FROZEN` convention (~/.claude/rules/ecc/common/coding-style.md:
immutable-by-construction, never mutated in place).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

_FROZEN = ConfigDict(frozen=True)


class IngestOutcome(StrEnum):
    """Which of docs/08 §4.1's four checks (or none) decided a
    `SnapshotIngestor` call's fate. Split finer than a bare accept/reject
    bool so a caller (and a test) can assert *which* check fired without
    string-matching `reason`."""

    ACCEPTED = "accepted"
    ACCEPTED_AFTER_RECOMPRESSION = "accepted_after_recompression"
    REJECTED_ENVELOPE = "rejected_envelope"  # check (a)
    REJECTED_OVERSIZE_DELTA = "rejected_oversize_delta"  # check (b), delta-only
    REJECTED_SCHEMA = "rejected_schema"  # check (c)
    REJECTED_SEQUENCE_GAP = "rejected_sequence_gap"  # check (d)


class IngestResult(BaseModel):
    """`SnapshotIngestor.ingest_*`'s return value. `reason` is a short,
    fixed diagnostic string (never raw payload content — this can reach
    logs) suitable for the "count metric" docs/08 §4.1 names for envelope
    failures."""

    model_config = _FROZEN

    session_id: str
    outcome: IngestOutcome
    accepted: bool
    seq: int | None = None
    needs_snapshot_request: bool = False
    dropped_rungs: tuple[int, ...] = ()
    reason: str | None = None


class RecompressResult(BaseModel):
    """`ContextCompressor.recompress_oversize`'s return value — the
    docs/07 §7 drop ladder applied to one screen_context/v1 IR."""

    model_config = _FROZEN

    ir: dict[str, Any]
    dropped_rungs: tuple[int, ...]
    estimated_tokens: int


class SnapshotSlot(BaseModel):
    """`ContextCompressor.render_snapshot_slot`'s return value — the
    prompt-ready text for slot 4 (docs/08 §5.1), plus the metadata
    `ContextBuilder` (T3) needs for its own span attributes
    (`snapshot_age_ms`, `stale`, `dropped_rungs`, docs/08 §5.3)."""

    model_config = _FROZEN

    text: str
    stale: bool
    age_s: int
    dropped_rungs: tuple[int, ...]
    estimated_tokens: int


class TimelineSlot(BaseModel):
    """`ContextCompressor.render_timeline_slot`'s return value — the
    prompt-ready text for slot 5 (docs/08 §5.1, worked example §2.4)."""

    model_config = _FROZEN

    text: str
    event_count: int
    estimated_tokens: int


__all__ = [
    "IngestOutcome",
    "IngestResult",
    "RecompressResult",
    "SnapshotSlot",
    "TimelineSlot",
]
