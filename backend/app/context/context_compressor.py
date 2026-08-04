"""`ContextCompressor` (docs/08-context-and-events.md §4.3) — the shaping
layer between raw stored `ctx:{session_id}` state and prompt-ready slot
text. Mechanical/string-only, no LLM: this class is meant to run inside the
15/40 ms p50/p95 `context.build` span docs/08 §4.3 budgets for it, though
opening that span is `ContextBuilder`'s job (T3), not this task's.

Three jobs, each its own public method:

1. **Staleness marking** (`render_snapshot_slot`) — `now - received_ts >
   30 s` gets a `note: ... may be stale` header (docs/08 §4.3).
2. **Slot truncation** (`render_snapshot_slot` / `render_timeline_slot`) —
   the snapshot is re-verified against the ≤300-token budget (defense in
   depth: the client's own docs/07 §7 ladder should have already kept it
   under budget, but "the server does not trust the client", docs/14) and
   the timeline is capped at the newest 15 events / rendered in the compact
   `docs/08 §2.4` line format.
3. **Oversize recovery** (`recompress_oversize`) — the docs/07 §7 drop
   ladder, rungs 1-6, re-estimating after each rung and stopping as soon as
   the result fits. This is the *same* method `SnapshotIngestor` calls on a
   check-(b) size-cap failure (docs/08 §4.1) — "one drop-ladder definition,
   two enforcement points" (docs/08 §4.3's own words) — so
   `SnapshotIngestor` takes a `ContextCompressor` as a constructor
   dependency rather than duplicating the ladder.

Judgment call — oversize *deltas* are out of `recompress_oversize`'s scope.
The docs/07 §7 ladder is defined entirely in terms of a full IR's
`components` array (list-item capping, nav-chrome roles, the rung-5/6
minimal/floor shapes); a `ctx.delta` payload's `changed`/`removed` arrays
are a structurally different, partial shape the ladder cannot be applied to
without risking an incomplete, silently-corrupting merge (truncating
`changed` arbitrarily could drop a component update the `removed` list — or
a later delta — depends on having actually happened). `SnapshotIngestor`
therefore does not route oversize deltas here at all; see that module's own
docstring for how it handles them instead (drop + gap-recovery round trip,
the same outcome an unrecoverable schema failure gets).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any, Final

import orjson

from app.context.models import RecompressResult, SnapshotSlot, TimelineSlot
from app.context.token_estimate import estimate_tokens

# docs/08 §4.3: "If `now - received_ts > 30 s`... the flag makes the prompt
# honest about that ambiguity."
STALENESS_THRESHOLD_S: Final = 30

# docs/08 §5.1 slot budgets (canon §8): slot 4 (snapshot) / slot 5 (timeline).
SNAPSHOT_TOKEN_BUDGET: Final = 300
TIMELINE_TOKEN_BUDGET: Final = 150
TIMELINE_MAX_EVENTS: Final = 15

# --- docs/07 §7 drop ladder ------------------------------------------------

_LIST_ROLE: Final = "list"
_LIST_ITEM_ROLE: Final = "list_item"
_MAX_LIST_ITEMS_PER_LIST: Final = 3  # rung 1
_LABEL_VALUE_MAX_CHARS: Final = 48  # rung 2
_ELLIPSIS: Final = "…"
_NAV_CHROME_ROLES: Final = frozenset({"tab", "toggle", "secondary_cta"})  # rung 4
_MINIMAL_ROLES: Final = frozenset(  # rung 5
    {"dialog", "error_banner", "snackbar", "amount_field", "recipient", "primary_cta"}
)
_SCREEN_NAME_ONLY_KEYS: Final = ("v", "screen", "flow", "last_api")  # rung 6


def _dumps(value: Any) -> str:
    """Compact JSON rendering used both for the chars/3.5 estimate and the
    final rendered slot text — `orjson`, matching the repo's existing
    serialization convention (`app/data/redis_client.py`)."""
    return orjson.dumps(value).decode()


def _truncate(text: str) -> str:
    if len(text) <= _LABEL_VALUE_MAX_CHARS:
        return text
    return text[: _LABEL_VALUE_MAX_CHARS - 1] + _ELLIPSIS


def _rung1_cap_list_items(ir: dict[str, Any]) -> dict[str, Any]:
    """Drop `list_item`s beyond the top 3 following each `list` (component
    identity/containment is positional — screen_context.v1.json's own
    description: "a list_item entry is one that immediately follows its
    list entry in reading order"). The `list` entry itself, with its
    `visible_count`/`total_count`, is always kept."""
    out: list[dict[str, Any]] = []
    kept_in_current_list = 0
    in_list = False
    for component in ir.get("components", []):
        role = component.get("role")
        if role == _LIST_ROLE:
            in_list = True
            kept_in_current_list = 0
            out.append(component)
        elif role == _LIST_ITEM_ROLE and in_list:
            kept_in_current_list += 1
            if kept_in_current_list <= _MAX_LIST_ITEMS_PER_LIST:
                out.append(component)
        else:
            in_list = False
            out.append(component)
    return {**ir, "components": out}


def _rung2_truncate_labels_and_values(ir: dict[str, Any]) -> dict[str, Any]:
    """Labels and values truncated to 48 chars with an ellipsis."""
    truncated = []
    for component in ir.get("components", []):
        new_component = dict(component)
        if isinstance(new_component.get("label"), str):
            new_component["label"] = _truncate(new_component["label"])
        if isinstance(new_component.get("value"), str):
            new_component["value"] = _truncate(new_component["value"])
        truncated.append(new_component)
    return {**ir, "components": truncated}


def _rung3_drop_disabled_non_primary(ir: dict[str, Any]) -> dict[str, Any]:
    """Disabled, non-primary interactive components dropped. Only
    `primary_cta`/`secondary_cta`/`toggle` ever carry `enabled` in the v1
    role vocabulary, so "non-primary interactive" reduces exactly to "has
    `enabled: false` and isn't `primary_cta`" — no role allowlist needed."""
    kept = [
        component
        for component in ir.get("components", [])
        if not (component.get("role") != "primary_cta" and component.get("enabled") is False)
    ]
    return {**ir, "components": kept}


def _rung4_drop_nav_chrome(ir: dict[str, Any]) -> dict[str, Any]:
    """Navigation chrome (`tab`, `toggle`, `secondary_cta`) dropped
    outright, regardless of state."""
    kept = [c for c in ir.get("components", []) if c.get("role") not in _NAV_CHROME_ROLES]
    return {**ir, "components": kept}


def _rung5_minimal(ir: dict[str, Any]) -> dict[str, Any]:
    """Minimal snapshot: only the six interruption/money-fact roles
    survive; every other top-level field is untouched."""
    kept = [c for c in ir.get("components", []) if c.get("role") in _MINIMAL_ROLES]
    return {**ir, "components": kept}


def _rung6_screen_name_only(ir: dict[str, Any]) -> dict[str, Any]:
    """The shared failure-mode floor (docs/07 §8): `v`, `screen`, `flow`,
    `last_api` are the four fields that actually carry information here —
    `components`/`last_action`/`dirty_fields`/`loading` reset to their
    empty-equivalents rather than being deleted outright.

    Judgment call, discovered by this task's own oversize-recovery round-
    trip test, not called out explicitly in docs/07 §7's summary table:
    that table lists rung 6's *contents* as literally `{"v", "screen",
    "flow", "last_api"}`, but `screen_context.v1.json`'s `required` list
    is `[v, screen, flow, components, last_action, last_api, dirty_fields,
    loading]` — all eight, unconditionally. A rung-6 result that dropped
    the other four keys entirely would no longer be a structurally valid
    `screen_context/v1` document, which matters concretely here: this is
    the *stored* `ctx:{session_id}` state after `SnapshotIngestor`'s
    check-(b) recompression (docs/08 §4.1's "route to ContextCompressor
    re-compression"), not just prompt-slot text discarded after one turn —
    a later `ctx.delta` merges onto it, and a validity check (either the
    inbound check (c), or this task's own post-merge judgment call) would
    reject every subsequent delta against a schema-invalid base state.
    Filling the empty-equivalents costs ~20 extra estimated tokens over
    the doc's literal "~25" figure (itself an approximation, not a pinned
    budget) — a small price for "the floor is still a valid document" over
    "the floor breaks every delta that follows it"."""
    minimal: dict[str, Any] = {key: ir[key] for key in _SCREEN_NAME_ONLY_KEYS if key in ir}
    minimal.setdefault("components", [])
    minimal.setdefault("last_action", None)
    minimal.setdefault("dirty_fields", [])
    minimal.setdefault("loading", False)
    return minimal


_RUNGS: Final[tuple[Callable[[dict[str, Any]], dict[str, Any]], ...]] = (
    _rung1_cap_list_items,
    _rung2_truncate_labels_and_values,
    _rung3_drop_disabled_non_primary,
    _rung4_drop_nav_chrome,
    _rung5_minimal,
    _rung6_screen_name_only,
)


def _render_event_line(event: dict[str, Any], *, now_ms: int) -> str:
    """One docs/08 §2.4-style compact timeline line: `-63s  nav →
    PaymentScreen`. Event-type-specific rendering per docs/08 §2.1's
    per-type field table; an unrecognized `type` (future taxonomy addition,
    docs/13 §9 additive-within-v1) falls back to `"{type} {name}"` rather
    than raising — this is prompt text, not a validated wire boundary."""
    ts = event.get("ts", now_ms)
    # `ceil`, not `round`: matches docs/08 §2.4's own worked example, where
    # an event 17.18s in the past renders as "-18s" — "at least N seconds
    # ago" rounds toward the safe (older-looking) direction, the same
    # over-estimate-is-safe convention `token_estimate.py` uses.
    age_s = max(0, math.ceil((now_ms - ts) / 1000))
    prefix = f"-{age_s}s" if age_s > 0 else "0s"

    event_type = event.get("type")
    name = event.get("name", "")
    if event_type == "nav":
        body = f"nav → {name}"
    elif event_type == "tap":
        body = f'tap "{name}"'
    elif event_type == "input":
        value = event.get("value")
        body = f'input {name} = "{value}"' if value is not None else f"input {name}"
    elif event_type == "api_error":
        status = event.get("status", "?")
        code = event.get("code", "")
        body = f"api_error {name} {status} {code}".rstrip()
    elif event_type == "dialog":
        body = f'dialog "{name}" {"shown" if event.get("visible") else "hidden"}'
    else:
        body = f"{event_type} {name}"
    return f"{prefix}  {body}"


class ContextCompressor:
    """Stateless — every method is a pure function of its arguments, so one
    instance is safely shared across sessions/requests (no per-session
    state to isolate)."""

    def estimate_tokens(self, text: str) -> int:
        return estimate_tokens(text)

    # -- Job 3: oversize recovery -----------------------------------------

    def recompress_oversize(self, ir: dict[str, Any]) -> RecompressResult:
        """Re-apply the docs/07 §7 drop ladder from rung 1, stopping as
        soon as the chars/3.5 estimate fits `SNAPSHOT_TOKEN_BUDGET` (or
        rung 6, the shared floor, whichever comes first)."""
        current = ir
        tokens = estimate_tokens(_dumps(current))
        if tokens <= SNAPSHOT_TOKEN_BUDGET:
            return RecompressResult(ir=current, dropped_rungs=(), estimated_tokens=tokens)

        applied: list[int] = []
        for rung_no, apply_rung in enumerate(_RUNGS, start=1):
            current = apply_rung(current)
            applied.append(rung_no)
            tokens = estimate_tokens(_dumps(current))
            if tokens <= SNAPSHOT_TOKEN_BUDGET:
                break
        return RecompressResult(ir=current, dropped_rungs=tuple(applied), estimated_tokens=tokens)

    # -- Jobs 1 + 2: staleness marking + snapshot slot truncation ---------

    def render_snapshot_slot(self, stored: dict[str, Any], *, now_ms: int) -> SnapshotSlot:
        """`stored` is `ctx:{session_id}`'s value as `SnapshotIngestor`
        writes it: the IR's own fields plus `seq`/`received_ts` (docs/08
        §4.1). Staleness is measured off `received_ts`, never the wall
        clock the IR itself might (but need not) carry."""
        received_ts = stored["received_ts"]
        ir = {k: v for k, v in stored.items() if k not in ("seq", "received_ts")}

        age_s = max(0, (now_ms - received_ts) // 1000)
        stale = age_s > STALENESS_THRESHOLD_S

        dropped_rungs: tuple[int, ...] = ()
        tokens = estimate_tokens(_dumps(ir))
        if tokens > SNAPSHOT_TOKEN_BUDGET:
            recompressed = self.recompress_oversize(ir)
            ir = recompressed.ir
            dropped_rungs = recompressed.dropped_rungs
            tokens = recompressed.estimated_tokens

        body = _dumps(ir)
        if stale:
            header = f"note: screen snapshot is {age_s}s old — may be stale"
            text = f"{header}\n{body}"
            tokens = estimate_tokens(text)
        else:
            text = body

        return SnapshotSlot(
            text=text,
            stale=stale,
            age_s=age_s,
            dropped_rungs=dropped_rungs,
            estimated_tokens=tokens,
        )

    # -- Job 2: timeline slot truncation -----------------------------------

    def render_timeline_slot(self, events: list[dict[str, Any]], *, now_ms: int) -> TimelineSlot:
        """Newest `TIMELINE_MAX_EVENTS`, oldest-first, in the docs/08 §2.4
        compact-line format. `events` is expected oldest-first (matching
        `EventLog.get_events`'s RPUSH-order convention) — the newest-N slice
        is taken off the end, order preserved."""
        newest = events[-TIMELINE_MAX_EVENTS:] if len(events) > TIMELINE_MAX_EVENTS else events
        lines = [f"[timeline — last {len(newest)} actions]"]
        lines.extend(_render_event_line(event, now_ms=now_ms) for event in newest)
        text = "\n".join(lines)
        return TimelineSlot(
            text=text, event_count=len(newest), estimated_tokens=estimate_tokens(text)
        )


__all__ = [
    "SNAPSHOT_TOKEN_BUDGET",
    "STALENESS_THRESHOLD_S",
    "TIMELINE_MAX_EVENTS",
    "TIMELINE_TOKEN_BUDGET",
    "ContextCompressor",
]
