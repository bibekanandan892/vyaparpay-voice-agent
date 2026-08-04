"""`SnapshotIngestor` — docs/08 §4.1's four checks, the REST + data-channel
entrypoints, the delta merge algorithm (docs/07 §6), and the post-merge
full-IR re-validation judgment call (see `snapshot_ingestor.py`'s own
module docstring, judgment call 3)."""

from __future__ import annotations

from typing import Any

import orjson
import pytest

from app.context.models import IngestOutcome
from app.context.redis_keys import CTX_TTL_SECONDS, _ctx_key
from app.context.snapshot_ingestor import SnapshotIngestor
from tests.context.conftest import load_fixture
from tests.support.fake_redis import FakeRedis

_NOW_MS = 1_784_536_500_000


def _base_snapshot_payload() -> dict[str, Any]:
    return load_fixture("ctx_snapshot_payment_decline")["payload"]


def _snapshot_envelope(
    seq: int, *, ts: int = 1_784_536_441_000, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "v": 1,
        "type": "ctx.snapshot",
        "seq": seq,
        "ts": ts,
        "payload": payload if payload is not None else _base_snapshot_payload(),
    }


def _delta_envelope(
    seq: int,
    base_seq: int,
    *,
    ts: int = 1_784_536_447_210,
    changed: list[dict[str, Any]] | None = None,
    removed: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "base_seq": base_seq,
        "changed": changed or [],
        "removed": removed or [],
    }
    if extra:
        payload.update(extra)
    return {"v": 1, "type": "ctx.delta", "seq": seq, "ts": ts, "payload": payload}


# ---------------------------------------------------------------------------
# Check (a): envelope shape
# ---------------------------------------------------------------------------


async def test_ingest_snapshot_rejects_wrong_v(ingestor: SnapshotIngestor) -> None:
    envelope = {**_snapshot_envelope(1), "v": 2}

    result = await ingestor.ingest_snapshot("sess_1", envelope)

    assert result.accepted is False
    assert result.outcome == IngestOutcome.REJECTED_ENVELOPE
    assert result.needs_snapshot_request is False


async def test_ingest_snapshot_rejects_bool_v_masquerading_as_one(
    ingestor: SnapshotIngestor,
) -> None:
    """Python's `True == 1` must not let a boolean satisfy `v == 1`."""
    envelope = {**_snapshot_envelope(1), "v": True}

    result = await ingestor.ingest_snapshot("sess_1", envelope)

    assert result.outcome == IngestOutcome.REJECTED_ENVELOPE


async def test_ingest_snapshot_rejects_a_delta_envelope(ingestor: SnapshotIngestor) -> None:
    envelope = _delta_envelope(1, 0)

    result = await ingestor.ingest_snapshot("sess_1", envelope)

    assert result.outcome == IngestOutcome.REJECTED_ENVELOPE


async def test_ingest_delta_rejects_a_snapshot_envelope(ingestor: SnapshotIngestor) -> None:
    envelope = _snapshot_envelope(1)

    result = await ingestor.ingest_delta("sess_1", envelope)

    assert result.outcome == IngestOutcome.REJECTED_ENVELOPE


async def test_ingest_snapshot_rejects_non_integer_seq(ingestor: SnapshotIngestor) -> None:
    envelope = {**_snapshot_envelope(1), "seq": "12"}

    result = await ingestor.ingest_snapshot("sess_1", envelope)

    assert result.outcome == IngestOutcome.REJECTED_ENVELOPE


async def test_ingest_snapshot_rejects_bool_seq(ingestor: SnapshotIngestor) -> None:
    envelope = {**_snapshot_envelope(1), "seq": True}

    result = await ingestor.ingest_snapshot("sess_1", envelope)

    assert result.outcome == IngestOutcome.REJECTED_ENVELOPE


async def test_ingest_snapshot_rejects_negative_seq(ingestor: SnapshotIngestor) -> None:
    envelope = {**_snapshot_envelope(-1)}

    result = await ingestor.ingest_snapshot("sess_1", envelope)

    assert result.outcome == IngestOutcome.REJECTED_ENVELOPE


async def test_ingest_snapshot_rejects_non_integer_ts(ingestor: SnapshotIngestor) -> None:
    envelope = {**_snapshot_envelope(1), "ts": "not-a-number"}

    result = await ingestor.ingest_snapshot("sess_1", envelope)

    assert result.outcome == IngestOutcome.REJECTED_ENVELOPE


async def test_ingest_snapshot_rejects_missing_payload(ingestor: SnapshotIngestor) -> None:
    envelope = {"v": 1, "type": "ctx.snapshot", "seq": 1, "ts": 1}

    result = await ingestor.ingest_snapshot("sess_1", envelope)

    assert result.outcome == IngestOutcome.REJECTED_ENVELOPE


async def test_envelope_rejection_writes_nothing_to_redis(
    ingestor: SnapshotIngestor, fake_redis: FakeRedis
) -> None:
    await ingestor.ingest_snapshot("sess_1", {**_snapshot_envelope(1), "v": 2})

    assert _ctx_key("sess_1") not in fake_redis.strings


# ---------------------------------------------------------------------------
# Check (b): size cap
# ---------------------------------------------------------------------------


async def test_ingest_snapshot_recompresses_an_oversized_payload(
    ingestor: SnapshotIngestor, fake_redis: FakeRedis
) -> None:
    dense = load_fixture("screen_context_settlements_dense")
    # x15 (not x5): the fixture's own serialized bulk needs to clear the
    # 8 KiB *byte* cap (check b) specifically, not just the compressor's
    # 300-*token* budget (a much lower bar an x5 repeat already exceeds,
    # per `test_context_compressor.py`'s own oversize test) — this test is
    # about `SnapshotIngestor`'s size-cap routing, not `ContextCompressor`'s
    # own budget check.
    oversized_payload = {**dense, "components": dense["components"] * 15}
    envelope = _snapshot_envelope(1, payload=oversized_payload)

    result = await ingestor.ingest_snapshot("sess_1", envelope)

    assert result.accepted is True
    assert result.outcome == IngestOutcome.ACCEPTED_AFTER_RECOMPRESSION
    assert result.dropped_rungs != ()
    # The recompressed, much smaller form is what actually lands in Redis.
    stored = orjson.loads(fake_redis.strings[_ctx_key("sess_1")])
    assert len(stored["components"]) < len(oversized_payload["components"])


async def test_ingest_delta_rejects_an_oversized_payload(
    ingestor: SnapshotIngestor, fake_redis: FakeRedis
) -> None:
    """Judgment call: oversized deltas are dropped, not recompressed (the
    docs/07 §7 ladder is IR-shaped, not delta-shaped) — see
    `snapshot_ingestor.py`'s module docstring, judgment call 2."""
    await ingestor.ingest_snapshot("sess_1", _snapshot_envelope(1))
    huge_changed = [
        {"role": "text_field", "label": f"f{i}", "value": "x" * 100} for i in range(200)
    ]
    envelope = _delta_envelope(2, 1, changed=huge_changed)

    result = await ingestor.ingest_delta("sess_1", envelope)

    assert result.accepted is False
    assert result.outcome == IngestOutcome.REJECTED_OVERSIZE_DELTA
    assert result.needs_snapshot_request is True
    # The previously-stored snapshot is untouched.
    stored = orjson.loads(fake_redis.strings[_ctx_key("sess_1")])
    assert stored["seq"] == 1


# ---------------------------------------------------------------------------
# Check (c): schema validation
# ---------------------------------------------------------------------------


async def test_ingest_snapshot_rejects_schema_invalid_payload(
    ingestor: SnapshotIngestor,
) -> None:
    bad_payload = {k: v for k, v in _base_snapshot_payload().items() if k != "screen"}
    envelope = _snapshot_envelope(1, payload=bad_payload)

    result = await ingestor.ingest_snapshot("sess_1", envelope)

    assert result.accepted is False
    assert result.outcome == IngestOutcome.REJECTED_SCHEMA
    assert result.needs_snapshot_request is True


async def test_ingest_delta_rejects_schema_invalid_payload(ingestor: SnapshotIngestor) -> None:
    await ingestor.ingest_snapshot("sess_1", _snapshot_envelope(1))
    envelope = _delta_envelope(2, 1)
    del envelope["payload"]["base_seq"]

    result = await ingestor.ingest_delta("sess_1", envelope)

    assert result.outcome == IngestOutcome.REJECTED_SCHEMA
    assert result.needs_snapshot_request is True


# ---------------------------------------------------------------------------
# Check (d): sequence continuity
# ---------------------------------------------------------------------------


async def test_ingest_snapshot_accepts_the_first_snapshot_for_a_fresh_session(
    ingestor: SnapshotIngestor,
) -> None:
    result = await ingestor.ingest_snapshot("sess_1", _snapshot_envelope(12))

    assert result.accepted is True
    assert result.outcome == IngestOutcome.ACCEPTED
    assert result.seq == 12


async def test_ingest_snapshot_rejects_a_stale_or_duplicate_seq(
    ingestor: SnapshotIngestor, fake_redis: FakeRedis
) -> None:
    await ingestor.ingest_snapshot("sess_1", _snapshot_envelope(12))

    result = await ingestor.ingest_snapshot("sess_1", _snapshot_envelope(12))

    assert result.accepted is False
    assert result.outcome == IngestOutcome.REJECTED_SEQUENCE_GAP
    assert result.needs_snapshot_request is True
    # The original state is left in place, not overwritten.
    assert orjson.loads(fake_redis.strings[_ctx_key("sess_1")])["seq"] == 12


async def test_ingest_snapshot_accepts_a_non_adjacent_gap_recovery_seq(
    ingestor: SnapshotIngestor,
) -> None:
    """docs/08 §3.3 step 3: the client's gap-recovery snapshot is "a fresh
    capture, not a replay" — its `seq` is whatever the client's counter is
    at *now*, not necessarily `last + 1`. A strict adjacency check would
    make the recovery message itself trigger another round trip."""
    await ingestor.ingest_snapshot("sess_1", _snapshot_envelope(12))

    result = await ingestor.ingest_snapshot("sess_1", _snapshot_envelope(50))

    assert result.accepted is True
    assert result.seq == 50


async def test_ingest_delta_accepts_a_matching_base_seq(ingestor: SnapshotIngestor) -> None:
    await ingestor.ingest_snapshot("sess_1", _snapshot_envelope(12))

    result = await ingestor.ingest_delta("sess_1", _delta_envelope(13, 12))

    assert result.accepted is True
    assert result.outcome == IngestOutcome.ACCEPTED
    assert result.seq == 13


async def test_ingest_delta_rejects_a_mismatched_base_seq(
    ingestor: SnapshotIngestor, fake_redis: FakeRedis
) -> None:
    await ingestor.ingest_snapshot("sess_1", _snapshot_envelope(12))

    result = await ingestor.ingest_delta("sess_1", _delta_envelope(13, 11))

    assert result.accepted is False
    assert result.outcome == IngestOutcome.REJECTED_SEQUENCE_GAP
    assert result.needs_snapshot_request is True
    assert orjson.loads(fake_redis.strings[_ctx_key("sess_1")])["seq"] == 12


async def test_ingest_delta_accepts_base_seq_that_skips_an_interleaved_event_seq(
    ingestor: SnapshotIngestor,
) -> None:
    """docs/08 §6's own worked trace: `ctx.event {"seq":16}` sits between a
    delta based on seq 15 and the next delta arriving at seq 17 —
    `base_seq` (not raw seq adjacency) is what a correct client computes
    against the last snapshot/delta, skipping the interleaved event's seq.
    See `snapshot_ingestor.py`'s module docstring, judgment call 1."""
    await ingestor.ingest_snapshot("sess_1", _snapshot_envelope(15))

    result = await ingestor.ingest_delta("sess_1", _delta_envelope(17, 15))

    assert result.accepted is True
    assert result.seq == 17


async def test_ingest_delta_rejects_when_no_prior_snapshot_exists(
    ingestor: SnapshotIngestor,
) -> None:
    result = await ingestor.ingest_delta("sess_1", _delta_envelope(1, 0))

    assert result.accepted is False
    assert result.outcome == IngestOutcome.REJECTED_SEQUENCE_GAP
    assert result.needs_snapshot_request is True


async def test_ingest_delta_rejects_a_replayed_seq_even_with_correct_base_seq(
    ingestor: SnapshotIngestor,
) -> None:
    await ingestor.ingest_snapshot("sess_1", _snapshot_envelope(12))
    await ingestor.ingest_delta("sess_1", _delta_envelope(13, 12))

    # Same base_seq=12 replayed, but seq 13 is no longer newer than the
    # now-stored seq 13.
    result = await ingestor.ingest_delta("sess_1", _delta_envelope(13, 12))

    assert result.accepted is False
    assert result.outcome == IngestOutcome.REJECTED_SEQUENCE_GAP


# ---------------------------------------------------------------------------
# docs/07 §6 merge algorithm
# ---------------------------------------------------------------------------


async def test_merge_replaces_a_changed_component_by_role_and_label(
    ingestor: SnapshotIngestor, fake_redis: FakeRedis
) -> None:
    await ingestor.ingest_snapshot("sess_1", _snapshot_envelope(12))

    await ingestor.ingest_delta(
        "sess_1",
        _delta_envelope(
            13,
            12,
            changed=[{"role": "dialog", "label": "Daily Limit Exceeded", "visible": False}],
        ),
    )

    stored = orjson.loads(fake_redis.strings[_ctx_key("sess_1")])
    dialogs = [c for c in stored["components"] if c["role"] == "dialog"]
    assert len(dialogs) == 1
    assert dialogs[0]["visible"] is False


async def test_merge_appends_a_changed_component_with_a_new_identity(
    ingestor: SnapshotIngestor, fake_redis: FakeRedis
) -> None:
    await ingestor.ingest_snapshot("sess_1", _snapshot_envelope(12))
    before_count = len(_base_snapshot_payload()["components"])

    await ingestor.ingest_delta(
        "sess_1",
        _delta_envelope(
            13, 12, changed=[{"role": "status_badge", "label": "Settlement", "value": "PENDING"}]
        ),
    )

    stored = orjson.loads(fake_redis.strings[_ctx_key("sess_1")])
    assert len(stored["components"]) == before_count + 1
    assert any(c["role"] == "status_badge" for c in stored["components"])


async def test_merge_removes_a_component_by_identity(
    ingestor: SnapshotIngestor, fake_redis: FakeRedis
) -> None:
    await ingestor.ingest_snapshot("sess_1", _snapshot_envelope(12))

    await ingestor.ingest_delta(
        "sess_1",
        _delta_envelope(
            13, 12, removed=[{"role": "snackbar", "label": "Payment Failed"}]
        ),
    )

    stored = orjson.loads(fake_redis.strings[_ctx_key("sess_1")])
    assert not any(c["role"] == "snackbar" for c in stored["components"])


async def test_merge_treats_a_label_change_as_remove_then_add(
    ingestor: SnapshotIngestor, fake_redis: FakeRedis
) -> None:
    """docs/07 §6: "A component whose *label* changed fails identity
    matching and appears as remove + add"."""
    await ingestor.ingest_snapshot("sess_1", _snapshot_envelope(12))

    await ingestor.ingest_delta(
        "sess_1",
        _delta_envelope(
            13,
            12,
            removed=[{"role": "dialog", "label": "Daily Limit Exceeded"}],
            changed=[
                {
                    "role": "dialog",
                    "label": "Daily Limit Exceeded (relabeled)",
                    "visible": True,
                }
            ],
        ),
    )

    stored = orjson.loads(fake_redis.strings[_ctx_key("sess_1")])
    dialog_labels = [c["label"] for c in stored["components"] if c["role"] == "dialog"]
    assert dialog_labels == ["Daily Limit Exceeded (relabeled)"]


async def test_merge_only_overwrites_top_level_fields_present_in_the_delta(
    ingestor: SnapshotIngestor, fake_redis: FakeRedis
) -> None:
    await ingestor.ingest_snapshot("sess_1", _snapshot_envelope(12))

    await ingestor.ingest_delta(
        "sess_1",
        _delta_envelope(
            13, 12, extra={"last_action": {"type": "tap", "target": "Dismiss", "ts": 999}}
        ),
    )

    stored = orjson.loads(fake_redis.strings[_ctx_key("sess_1")])
    assert stored["last_action"] == {"type": "tap", "target": "Dismiss", "ts": 999}
    # `loading`/`dirty_fields` weren't in the delta — untouched from the base.
    base = _base_snapshot_payload()
    assert stored["loading"] == base["loading"]
    assert stored["dirty_fields"] == base["dirty_fields"]


# ---------------------------------------------------------------------------
# Judgment call 3: post-merge full-IR re-validation
# ---------------------------------------------------------------------------


async def test_ingest_delta_rejects_a_semantically_incomplete_merge_result(
    ingestor: SnapshotIngestor, fake_redis: FakeRedis
) -> None:
    """`ctx_delta.v1.json`'s `changed_component` only validates
    `role`+`label` at the schema level — a `dialog` missing `visible`
    passes check (c) but must fail the post-merge full-IR check. The
    previously-stored (complete) state must be left untouched."""
    await ingestor.ingest_snapshot("sess_1", _snapshot_envelope(12))
    # A brand-new identity (not already present), so it lands via APPEND —
    # the resulting merged IR then has a `dialog` entry with no `visible`.
    incomplete_dialog = {"role": "dialog", "label": "Brand New Dialog"}

    result = await ingestor.ingest_delta(
        "sess_1", _delta_envelope(13, 12, changed=[incomplete_dialog])
    )

    assert result.accepted is False
    assert result.outcome == IngestOutcome.REJECTED_SCHEMA
    assert result.needs_snapshot_request is True
    stored = orjson.loads(fake_redis.strings[_ctx_key("sess_1")])
    assert stored["seq"] == 12  # unchanged — the incomplete merge was never written
    assert not any(c.get("label") == "Brand New Dialog" for c in stored["components"])


# ---------------------------------------------------------------------------
# REST entrypoint — POST /v1/sessions' screen_context field
# ---------------------------------------------------------------------------


async def test_ingest_initial_snapshot_rejects_the_wrong_version_marker(
    ingestor: SnapshotIngestor,
) -> None:
    payload = {**_base_snapshot_payload(), "v": "screen_context/v2"}

    result = await ingestor.ingest_initial_snapshot("sess_1", payload, received_at_ms=_NOW_MS)

    assert result.accepted is False
    assert result.outcome == IngestOutcome.REJECTED_ENVELOPE


async def test_ingest_initial_snapshot_accepts_and_seeds_seq_zero(
    ingestor: SnapshotIngestor, fake_redis: FakeRedis
) -> None:
    result = await ingestor.ingest_initial_snapshot(
        "sess_1", _base_snapshot_payload(), received_at_ms=_NOW_MS
    )

    assert result.accepted is True
    assert result.seq == 0
    stored = orjson.loads(fake_redis.strings[_ctx_key("sess_1")])
    assert stored["seq"] == 0
    assert stored["received_ts"] == _NOW_MS
    assert stored["screen"] == "PaymentScreen"


async def test_ingest_initial_snapshot_lets_a_first_delta_reference_seq_zero(
    ingestor: SnapshotIngestor,
) -> None:
    """Exercises judgment call 4: the REST-ingested snapshot's genesis
    anchor is `seq=0`, so the first in-call delta must diff against it."""
    await ingestor.ingest_initial_snapshot(
        "sess_1", _base_snapshot_payload(), received_at_ms=_NOW_MS
    )

    result = await ingestor.ingest_delta("sess_1", _delta_envelope(1, 0))

    assert result.accepted is True
    assert result.seq == 1


async def test_ingest_initial_snapshot_recompresses_an_oversized_ir(
    ingestor: SnapshotIngestor,
) -> None:
    dense = load_fixture("screen_context_settlements_dense")
    oversized = {**dense, "components": dense["components"] * 15}  # clears the 8 KiB byte cap

    result = await ingestor.ingest_initial_snapshot("sess_1", oversized, received_at_ms=_NOW_MS)

    assert result.accepted is True
    assert result.outcome == IngestOutcome.ACCEPTED_AFTER_RECOMPRESSION
    assert result.dropped_rungs != ()


async def test_ingest_initial_snapshot_rejects_schema_invalid_ir(
    ingestor: SnapshotIngestor,
) -> None:
    bad_payload = {k: v for k, v in _base_snapshot_payload().items() if k != "loading"}

    result = await ingestor.ingest_initial_snapshot("sess_1", bad_payload, received_at_ms=_NOW_MS)

    assert result.accepted is False
    assert result.outcome == IngestOutcome.REJECTED_SCHEMA


# ---------------------------------------------------------------------------
# Storage shape / TTL
# ---------------------------------------------------------------------------


async def test_accepted_snapshot_is_stored_under_the_canon_ctx_key_with_ttl(
    ingestor: SnapshotIngestor, fake_redis: FakeRedis
) -> None:
    await ingestor.ingest_snapshot("sess_1", _snapshot_envelope(12))

    assert "ctx:sess_1" in fake_redis.strings
    assert fake_redis.ttls["ctx:sess_1"] == CTX_TTL_SECONDS


async def test_ctx_key_rejects_a_colon_in_session_id(ingestor: SnapshotIngestor) -> None:
    with pytest.raises(ValueError, match="session_id"):
        await ingestor.ingest_snapshot("sess:evil", _snapshot_envelope(12))
