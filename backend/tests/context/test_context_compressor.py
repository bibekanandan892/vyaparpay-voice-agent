"""`ContextCompressor`'s three jobs (docs/08 §4.3): staleness marking, slot
truncation (snapshot + timeline), oversize recovery (the docs/07 §7 drop
ladder)."""

from __future__ import annotations

from typing import Any

from app.context.context_compressor import (
    SNAPSHOT_TOKEN_BUDGET,
    STALENESS_THRESHOLD_S,
    TIMELINE_MAX_EVENTS,
    ContextCompressor,
)
from app.context.schema_validation import is_valid, load_schema
from tests.context.conftest import load_fixture

_NOW_MS = 1_784_536_500_000


def _stored(ir: dict[str, Any], *, seq: int = 12, received_ts: int) -> dict[str, Any]:
    return {**ir, "seq": seq, "received_ts": received_ts}


# ---------------------------------------------------------------------------
# Job 1: staleness marking
# ---------------------------------------------------------------------------


def test_snapshot_within_freshness_window_is_not_stale(compressor: ContextCompressor) -> None:
    ir = load_fixture("screen_context_payment_decline")
    received_ts = _NOW_MS - (STALENESS_THRESHOLD_S - 5) * 1000

    slot = compressor.render_snapshot_slot(_stored(ir, received_ts=received_ts), now_ms=_NOW_MS)

    assert slot.stale is False
    assert "may be stale" not in slot.text


def test_snapshot_older_than_30s_is_marked_stale_with_age_header(
    compressor: ContextCompressor,
) -> None:
    ir = load_fixture("screen_context_payment_decline")
    received_ts = _NOW_MS - 47_000

    slot = compressor.render_snapshot_slot(_stored(ir, received_ts=received_ts), now_ms=_NOW_MS)

    assert slot.stale is True
    assert slot.age_s == 47
    assert slot.text.startswith("note: screen snapshot is 47s old — may be stale\n")


def test_snapshot_exactly_at_the_threshold_is_not_yet_stale(
    compressor: ContextCompressor,
) -> None:
    """docs/08 §4.3: `now - received_ts > 30s` — strictly greater than."""
    ir = load_fixture("screen_context_payment_decline")
    received_ts = _NOW_MS - STALENESS_THRESHOLD_S * 1000

    slot = compressor.render_snapshot_slot(_stored(ir, received_ts=received_ts), now_ms=_NOW_MS)

    assert slot.stale is False


# ---------------------------------------------------------------------------
# Job 2: slot truncation — snapshot
# ---------------------------------------------------------------------------


def test_within_budget_snapshot_renders_without_dropping_anything(
    compressor: ContextCompressor,
) -> None:
    ir = load_fixture("screen_context_payment_decline")

    slot = compressor.render_snapshot_slot(_stored(ir, received_ts=_NOW_MS), now_ms=_NOW_MS)

    assert slot.dropped_rungs == ()
    assert slot.estimated_tokens <= SNAPSHOT_TOKEN_BUDGET
    assert '"screen":"PaymentScreen"' in slot.text or '"screen": "PaymentScreen"' in slot.text


def test_render_snapshot_slot_recompresses_when_over_budget_defense_in_depth(
    compressor: ContextCompressor,
) -> None:
    """docs/08 §4.3: "snapshot re-verified ≤300 (defense in depth — the
    client already enforces this, but the server does not trust the
    client)." A stored IR that is itself over budget must still come out
    under budget."""
    dense = load_fixture("screen_context_settlements_dense")
    oversized = {
        **dense,
        "components": dense["components"] * 5,  # far over 300 tokens
    }

    slot = compressor.render_snapshot_slot(_stored(oversized, received_ts=_NOW_MS), now_ms=_NOW_MS)

    assert slot.dropped_rungs != ()
    assert slot.estimated_tokens <= SNAPSHOT_TOKEN_BUDGET


# ---------------------------------------------------------------------------
# Job 2: slot truncation — timeline
# ---------------------------------------------------------------------------


def _event(event_type: str, name: str, ts: int, **extra: Any) -> dict[str, Any]:
    return {"type": event_type, "name": name, "ts": ts, **extra}


def test_timeline_caps_at_the_newest_15_events(compressor: ContextCompressor) -> None:
    events = [_event("tap", str(i), _NOW_MS - (20 - i) * 1000) for i in range(20)]

    slot = compressor.render_timeline_slot(events, now_ms=_NOW_MS)

    assert slot.event_count == TIMELINE_MAX_EVENTS
    # Each rendered line is `-Ns  tap "name"` — the name is the text
    # between the two double quotes.
    rendered_names = [line.split('"')[1] for line in slot.text.splitlines()[1:]]
    assert rendered_names == [str(i) for i in range(5, 20)]


def test_timeline_under_the_cap_keeps_every_event(compressor: ContextCompressor) -> None:
    events = [_event("tap", "only", _NOW_MS)]

    slot = compressor.render_timeline_slot(events, now_ms=_NOW_MS)

    assert slot.event_count == 1


def test_timeline_header_names_the_rendered_count() -> None:
    compressor = ContextCompressor()
    events = [_event("tap", "a", _NOW_MS), _event("tap", "b", _NOW_MS)]

    slot = compressor.render_timeline_slot(events, now_ms=_NOW_MS)

    assert slot.text.splitlines()[0] == "[timeline — last 2 actions]"


def test_canonical_eight_event_timeline_renders_the_docs_08_worked_example(
    compressor: ContextCompressor,
) -> None:
    """docs/08 §2.4's own worked example — same eight events, checked for
    the same semantic content per line (exact column alignment is
    documentation styling, not a wire contract, so this asserts substrings
    rather than a byte-identical block)."""
    now_ms = 1_784_536_458_000  # "Call Support" tap, the 0s anchor
    events = [
        _event("nav", "PaymentScreen", 1_784_536_395_000, **{"from": "DashboardScreen"}),
        _event("input", "amount", 1_784_536_417_000, value="₹245"),
        _event("tap", "Amazon Business", 1_784_536_428_000, screen="PaymentScreen"),
        _event("tap", "Pay Now", 1_784_536_440_000, screen="PaymentScreen"),
        _event(
            "api_error",
            "POST /payments",
            1_784_536_440_820,
            status=402,
            code="DAILY_LIMIT_EXCEEDED",
        ),
        _event("dialog", "Daily Limit Exceeded", 1_784_536_440_900, visible=True),
        _event("nav", "HelpScreen", 1_784_536_452_000, **{"from": "PaymentScreen"}),
        _event("tap", "Call Support", 1_784_536_458_000, screen="HelpScreen"),
    ]

    slot = compressor.render_timeline_slot(events, now_ms=now_ms)
    lines = slot.text.splitlines()[1:]

    assert lines[0] == "-63s  nav → PaymentScreen"
    assert lines[1] == '-41s  input amount = "₹245"'
    assert lines[2] == '-30s  tap "Amazon Business"'
    assert lines[3] == '-18s  tap "Pay Now"'
    assert lines[4] == "-18s  api_error POST /payments 402 DAILY_LIMIT_EXCEEDED"
    assert lines[5] == '-18s  dialog "Daily Limit Exceeded" shown'
    assert lines[6] == "-6s  nav → HelpScreen"
    assert lines[7] == '0s  tap "Call Support"'


def test_input_event_without_value_omits_the_equals_clause(
    compressor: ContextCompressor,
) -> None:
    """docs/08 §2.1: sensitive-field input events omit `value` entirely —
    the rendered line must not assert a value it doesn't have."""
    events = [_event("input", "pin", _NOW_MS)]

    slot = compressor.render_timeline_slot(events, now_ms=_NOW_MS)

    assert slot.text.splitlines()[1] == "0s  input pin"


def test_dialog_hidden_renders_distinctly_from_shown(compressor: ContextCompressor) -> None:
    events = [_event("dialog", "Daily Limit Exceeded", _NOW_MS, visible=False)]

    slot = compressor.render_timeline_slot(events, now_ms=_NOW_MS)

    assert "hidden" in slot.text
    assert "shown" not in slot.text


def test_unrecognized_event_type_falls_back_to_type_and_name(
    compressor: ContextCompressor,
) -> None:
    """docs/13 §9: additive-within-v1 — a future event type must render
    *something* useful rather than raising."""
    events = [_event("scroll", "settlements_list", _NOW_MS)]

    slot = compressor.render_timeline_slot(events, now_ms=_NOW_MS)

    assert "scroll settlements_list" in slot.text


# ---------------------------------------------------------------------------
# Job 3: oversize recovery — the docs/07 §7 drop ladder
# ---------------------------------------------------------------------------


def test_already_within_budget_ir_is_returned_unchanged(compressor: ContextCompressor) -> None:
    ir = load_fixture("screen_context_payment_decline")

    result = compressor.recompress_oversize(ir)

    assert result.dropped_rungs == ()
    assert result.ir == ir


def test_rung1_alone_can_bring_a_list_heavy_ir_under_budget(
    compressor: ContextCompressor,
) -> None:
    """A list with many `list_item`s is the rung-1 case: capping to 3 per
    list should be enough on its own for a screen that is otherwise
    small."""
    ir = {
        "v": "screen_context/v1",
        "screen": "SettlementsScreen",
        "flow": "settlements",
        "components": [
            {"role": "list", "label": "Batches", "visible_count": 30, "total_count": 47},
            *[
                {"role": "list_item", "label": f"Batch {i}", "value": "₹1,000"}
                for i in range(30)
            ],
        ],
        "last_action": None,
        "last_api": None,
        "dirty_fields": [],
        "loading": False,
    }

    result = compressor.recompress_oversize(ir)

    assert result.dropped_rungs == (1,)
    list_items = [c for c in result.ir["components"] if c["role"] == "list_item"]
    assert len(list_items) == 3
    # The `list` entry itself survives with its counts intact.
    assert any(c["role"] == "list" and c["total_count"] == 47 for c in result.ir["components"])
    assert result.estimated_tokens <= SNAPSHOT_TOKEN_BUDGET


def test_full_ladder_round_trip_bottoms_out_at_the_screen_name_only_floor(
    compressor: ContextCompressor,
) -> None:
    """A genuinely pathological IR must walk every rung and land on the
    docs/07 §8 shared floor: `{v, screen, flow, last_api}` only.

    Deliberately built from `amount_field` components — a rung-5 "minimal
    set" role — so that rungs 1-4 (none of which apply: no lists, labels
    already short after rung 2's truncation still leaves plenty of bulk,
    nothing disabled, no nav chrome) AND rung 5 (which keeps every
    `amount_field`, only *filtering* roles, never capping the count within
    a kept role) all fail to bring it under budget on their own — only
    rung 6's total wipe does. This is what makes it a genuine six-rung
    round trip rather than an early exit at rung 3 or 5.
    """
    long_value = "₹" + "9" * 100
    components = [
        {"role": "amount_field", "label": f"Line item {i}", "value": long_value}
        for i in range(40)
    ]
    ir = {
        "v": "screen_context/v1",
        "screen": "SettlementsScreen",
        "flow": "settlements",
        "components": components,
        "last_action": {"type": "tap", "target": "x", "ts": 1},
        "last_api": {
            "method": "GET",
            "path": "/settlements",
            "status": 200,
            "error_code": "",
        },
        "dirty_fields": ["amount"],
        "loading": True,
    }

    result = compressor.recompress_oversize(ir)

    assert result.dropped_rungs == (1, 2, 3, 4, 5, 6)
    # The four informative fields survive; the other four required fields
    # reset to their empty-equivalents rather than being deleted outright
    # (see `_rung6_screen_name_only`'s docstring — the floor must stay a
    # structurally valid screen_context/v1 document, since it's what
    # actually gets stored and later merged onto).
    assert set(result.ir.keys()) == {
        "v",
        "screen",
        "flow",
        "last_api",
        "components",
        "last_action",
        "dirty_fields",
        "loading",
    }
    assert result.ir["screen"] == "SettlementsScreen"
    assert result.ir["components"] == []
    assert result.ir["last_action"] is None
    assert result.ir["dirty_fields"] == []
    assert result.ir["loading"] is False
    assert result.estimated_tokens <= SNAPSHOT_TOKEN_BUDGET
    # The floor is meant to be stored and merged onto by a later delta
    # (see the docstring above) — it must stay a structurally valid
    # screen_context/v1 document, not just small.
    assert is_valid(result.ir, load_schema("screen_context.v1"))


def test_rung3_drops_disabled_secondary_cta_but_keeps_disabled_primary_cta(
    compressor: ContextCompressor,
) -> None:
    """Rung 3 is scoped to "disabled, NON-primary" — a disabled
    `primary_cta` must survive this rung (it's the money-fact/interruption
    role docs/07 §7 protects the longest), while every disabled
    `secondary_cta` is dropped outright. Forty short-labeled
    `secondary_cta`s (well over budget on count alone, but each too short
    for rung 2 to touch) is enough on its own for rung 3 to resolve it —
    deterministic, unlike sizing this off byte-math shared with other
    tests."""
    ir = {
        "v": "screen_context/v1",
        "screen": "PaymentScreen",
        "flow": "vendor_payment",
        "components": [
            {"role": "primary_cta", "label": "Pay Now", "enabled": False},
            *[
                {"role": "secondary_cta", "label": f"Option {i}", "enabled": False}
                for i in range(40)
            ],
        ],
        "last_action": None,
        "last_api": None,
        "dirty_fields": [],
        "loading": False,
    }

    result = compressor.recompress_oversize(ir)

    assert result.dropped_rungs == (1, 2, 3)
    roles_after = [c["role"] for c in result.ir["components"]]
    assert roles_after == ["primary_cta"]
