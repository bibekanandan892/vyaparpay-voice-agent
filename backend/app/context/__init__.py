"""Backend context-ingestion pipeline (docs/08-context-and-events.md §4):
`SnapshotIngestor` (validate + store snapshots/deltas), `EventLog` (store
the event timeline), `ContextCompressor` (shape stored state into
prompt-ready slot text). Components only — nothing here is wired into an
HTTP route or `app/agent/context_builder.py`; that's a separate,
downstream task's job (this package's own module docstrings say so
explicitly)."""

from __future__ import annotations

from app.context.context_compressor import ContextCompressor
from app.context.event_log import EventLog
from app.context.models import (
    IngestOutcome,
    IngestResult,
    RecompressResult,
    SnapshotSlot,
    TimelineSlot,
)
from app.context.snapshot_ingestor import SnapshotIngestor

__all__ = [
    "ContextCompressor",
    "EventLog",
    "IngestOutcome",
    "IngestResult",
    "RecompressResult",
    "SnapshotIngestor",
    "SnapshotSlot",
    "TimelineSlot",
]
