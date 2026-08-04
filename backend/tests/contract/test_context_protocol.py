"""Contract tests for the Phase-4 T1 screen-context protocol freeze
(docs/07-ui-semantic-context.md, docs/08-context-and-events.md §2.1/§4.1,
docs/13-api-contracts.md §4/§9).

Covers the three new schemas this task owns:

- `protocol/schemas/screen_context.v1.json` -- the `screen_context/v1` IR
  (docs/07 §3), used verbatim as: the `session_create_request.v1.json`
  `screen_context` field, the `ctx.snapshot` data-channel payload, and the
  freestanding fixture in `protocol/fixtures/context/`.
- `protocol/schemas/app_event.v1.json` -- one `app_event/v1` timeline entry
  (docs/08 §2.1), used verbatim as: `session_create_request.v1.json`'s
  `recent_events` items, the `ctx.event` data-channel payload, and the
  freestanding fixture.
- `protocol/schemas/ctx_delta.v1.json` -- the `ctx.delta` payload (docs/07
  §6, docs/13 §4): the one payload shape with no other authority in
  `protocol/`.

**Fixture location.** New fixtures live under `protocol/fixtures/context/`,
a subdirectory of the shared fixtures directory, NOT flat alongside the
existing voice-protocol fixtures. This is deliberate:
`tests/contract/test_voice_protocol.py::test_every_fixture_file_is_wired_into_this_suite`
does a non-recursive `glob("*.json")` over `protocol/fixtures/` and asserts
the on-disk set is *exactly* its own registry -- dropping new files in flat
would break that already-passing test, which this task does not own and
must not edit. A subdirectory is invisible to a non-recursive glob, so the
two suites can never collide; this file's fixtures are additionally
prefixed `screen_context_` / `app_event_` / `ctx_` per the task brief, as
defense in depth.

**Layer 1 only -- fixture <-> schema (standalone, always runs).** Mirrors
`test_voice_protocol.py`'s Layer 1 exactly: `jsonschema` is deliberately
not a backend dependency, so validation is the same hand-rolled structural
walker, implementing the same keyword subset:
`$ref` (local `#/$defs/...` only), `type`, `const`, `enum`, `pattern`,
`minimum`, `properties`/`required`/`additionalProperties`,
`items`/`minItems`, `oneOf`, `anyOf`. It is duplicated into this file
rather than imported from `test_voice_protocol.py` (per this task's file
ownership: that file belongs to another in-flight task and must not be
edited or imported from, since a shared-helpers refactor is exactly the
kind of cross-file coupling that would need coordinating with that task).

**Two additions beyond the upstream keyword subset: `maxLength` and
`maxItems`.** The voice protocol (signaling, data-channel envelopes,
session REST) never needed a size cap, so its walker never grew one. The
screen-context IR has two hard, doc-stated caps that this task is
specifically asked to encode as schema constraints where expressible
(docs/07 §3: 20-component hard cap; docs/07 §5 rule 6: values normalized
and length-capped at 120 chars -- read here as covering `label` too, since
rule 2's text-run merge produces labels from the same untrusted screen text
as values, and the injection-surface rationale applies equally to both;
see `protocol/schemas/screen_context.v1.json`'s own top-level description
for the full citation). `minLength` was deliberately not added: no doc
states a minimum, and where non-emptiness incidentally matters the existing
`pattern` keyword already covers it -- not needed here.

**No Layer 2 (Pydantic round-trip).** Unlike `test_voice_protocol.py`, this
file does not attempt a `backend/app/domain/*` round-trip: those models are
owned by a downstream task (the backend context-ingestion wiring), not this
protocol-freeze task, and this task must not touch `backend/app/**`. The
schemas and fixtures here are exactly what that downstream task builds
against.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

_PROTOCOL_DIR = Path(__file__).resolve().parents[3] / "protocol"
_SCHEMAS_DIR = _PROTOCOL_DIR / "schemas"
_FIXTURES_DIR = _PROTOCOL_DIR / "fixtures" / "context"

_CORE_ROLES: tuple[str, ...] = (
    "amount_field", "recipient", "primary_cta", "secondary_cta", "text_field",
    "list", "list_item", "dialog", "snackbar", "tab", "toggle",
    "balance_display", "status_badge", "error_banner", "alert_banner", "image",
)

_SCREEN_CONTEXT_COMPONENT_DEF_NAMES: tuple[str, ...] = (
    "amount_field_component", "recipient_component", "primary_cta_component",
    "secondary_cta_component", "text_field_component", "list_component",
    "list_item_component", "dialog_component", "snackbar_component", "tab_component",
    "toggle_component", "balance_display_component", "status_badge_component",
    "error_banner_component", "alert_banner_component", "image_component",
)

# fixture basename -> the app_event/v1 `type` it must carry (docs/08 §2.4
# canonical timeline).
_APP_EVENT_FIXTURES: dict[str, str] = {
    "app_event_nav": "nav",
    "app_event_tap": "tap",
    "app_event_input": "input",
    "app_event_api_error": "api_error",
    "app_event_dialog": "dialog",
}

# fixture basename -> the envelope `type` it must carry (docs/13 §4).
_CTX_ENVELOPE_FIXTURES: dict[str, str] = {
    "ctx_snapshot_payment_decline": "ctx.snapshot",
    "ctx_delta_dialog_dismissed": "ctx.delta",
    "ctx_request_snapshot": "ctx.request_snapshot",
    "ctx_event_tap_dismiss": "ctx.event",
}

_SCREEN_CONTEXT_FIXTURES: list[str] = [
    "screen_context_payment_decline",
    "screen_context_settlements_dense",
]

_ALL_FIXTURES: list[str] = (
    _SCREEN_CONTEXT_FIXTURES + list(_APP_EVENT_FIXTURES) + list(_CTX_ENVELOPE_FIXTURES)
)


def _load_fixture(name: str) -> Any:
    return json.loads((_FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _load_schema(name: str) -> dict[str, Any]:
    return json.loads((_SCHEMAS_DIR / f"{name}.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Hand-rolled structural walker -- duplicated from test_voice_protocol.py
# (see module docstring for why), extended with maxLength/maxItems.
# ---------------------------------------------------------------------------


def _type_name(value: Any) -> str:
    """JSON type of a decoded value. bool first: bool is an int subclass in
    Python but a distinct type in JSON."""
    if isinstance(value, bool):
        return "boolean"
    if value is None:
        return "null"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    assert isinstance(value, dict), f"non-JSON value {value!r}"
    return "object"


def _json_equal(a: Any, b: Any) -> bool:
    """`==` gated on matching JSON type first -- plain `==`/`in` would let
    Python's bool-is-an-int-subclass equivalence (`True == 1`) satisfy a
    `const`/`enum` that names an integer, or vice versa, which JSON
    Schema's own type system never allows."""
    a_type, b_type = _type_name(a), _type_name(b)
    numeric = {"integer", "number"}
    if a_type != b_type and not (a_type in numeric and b_type in numeric):
        return False
    return a == b


def _check_scalars(value: Any, schema: dict[str, Any], path: str) -> list[str]:
    errs: list[str] = []
    if "const" in schema and not _json_equal(value, schema["const"]):
        errs.append(f"{path}: {value!r} != const {schema['const']!r}")
    if "enum" in schema and not any(_json_equal(value, member) for member in schema["enum"]):
        errs.append(f"{path}: {value!r} not in enum {schema['enum']!r}")
    if "pattern" in schema and isinstance(value, str) and not re.search(schema["pattern"], value):
        errs.append(f"{path}: {value!r} does not match pattern {schema['pattern']!r}")
    is_number = isinstance(value, int | float) and not isinstance(value, bool)
    if "minimum" in schema and is_number and value < schema["minimum"]:
        errs.append(f"{path}: {value!r} < minimum {schema['minimum']!r}")
    # -- addition beyond test_voice_protocol.py's subset (see module docstring) --
    if "maxLength" in schema and isinstance(value, str) and len(value) > schema["maxLength"]:
        errs.append(f"{path}: length {len(value)} > maxLength {schema['maxLength']!r}")
    return errs


def _check_object(value: dict[str, Any], schema: dict[str, Any], defs: dict, path: str) -> list:
    errs: list[str] = []
    props: dict[str, Any] = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in value:
            errs.append(f"{path}: missing required property {key!r}")
    for key, sub_schema in props.items():
        if key in value:
            errs.extend(_errors(value[key], sub_schema, defs, f"{path}.{key}"))
    if schema.get("additionalProperties") is False:
        extra = set(value) - set(props)
        if extra:
            errs.append(f"{path}: additionalProperties false but got {sorted(extra)!r}")
    return errs


def _check_array(value: list[Any], schema: dict[str, Any], defs: dict, path: str) -> list[str]:
    errs: list[str] = []
    if "minItems" in schema and len(value) < schema["minItems"]:
        errs.append(f"{path}: {len(value)} items < minItems {schema['minItems']}")
    # -- addition beyond test_voice_protocol.py's subset (see module docstring) --
    if "maxItems" in schema and len(value) > schema["maxItems"]:
        errs.append(f"{path}: {len(value)} items > maxItems {schema['maxItems']}")
    if "items" in schema:
        for i, item in enumerate(value):
            errs.extend(_errors(item, schema["items"], defs, f"{path}[{i}]"))
    return errs


def _errors(value: Any, schema: dict[str, Any], defs: dict[str, Any], path: str) -> list[str]:
    """All violations of `schema` by `value`, as `path: reason` strings."""
    if "$ref" in schema:
        ref_name = schema["$ref"].rsplit("/", 1)[-1]
        assert ref_name in defs, f"{path}: dangling $ref {schema['$ref']!r}"
        merged = {**defs[ref_name], **{k: v for k, v in schema.items() if k != "$ref"}}
        return _errors(value, merged, defs, path)

    errs: list[str] = []
    if "type" in schema:
        allowed = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        actual = _type_name(value)
        if actual not in allowed and not (actual == "integer" and "number" in allowed):
            # Wrong shape entirely -- nested keyword errors would only add noise.
            return [f"{path}: type {actual!r} not in {allowed!r}"]
    errs.extend(_check_scalars(value, schema, path))
    if isinstance(value, dict):
        errs.extend(_check_object(value, schema, defs, path))
    elif isinstance(value, list):
        errs.extend(_check_array(value, schema, defs, path))
    if "oneOf" in schema:
        matched = sum(1 for branch in schema["oneOf"] if not _errors(value, branch, defs, path))
        if matched != 1:
            errs.append(f"{path}: oneOf matched {matched} branches, need exactly 1")
    if "anyOf" in schema and all(_errors(value, b, defs, path) for b in schema["anyOf"]):
        errs.append(f"{path}: no anyOf branch matched")
    return errs


def _assert_valid(instance: Any, schema: dict[str, Any]) -> None:
    errs = _errors(instance, schema, schema.get("$defs", {}), "$")
    assert not errs, "schema violations:\n" + "\n".join(errs)


# ---------------------------------------------------------------------------
# Positive: every fixture validates against its schema
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", _SCREEN_CONTEXT_FIXTURES)
def test_screen_context_fixture_validates(fixture: str) -> None:
    _assert_valid(_load_fixture(fixture), _load_schema("screen_context.v1"))


@pytest.mark.parametrize(("fixture", "expected_type"), sorted(_APP_EVENT_FIXTURES.items()))
def test_app_event_fixture_validates(fixture: str, expected_type: str) -> None:
    event = _load_fixture(fixture)
    assert event["type"] == expected_type
    _assert_valid(event, _load_schema("app_event.v1"))


@pytest.mark.parametrize(("fixture", "expected_type"), sorted(_CTX_ENVELOPE_FIXTURES.items()))
def test_ctx_envelope_fixture_validates(fixture: str, expected_type: str) -> None:
    """Envelope-level check only (docs/13 §4 shape): v/type/seq/ts/payload,
    using the EXISTING data_channel_envelope.v1.json this task does not own
    or modify. Payload-level depth is checked separately below against the
    schemas this task does own."""
    frame = _load_fixture(fixture)
    assert frame["type"] == expected_type
    _assert_valid(frame, _load_schema("data_channel_envelope.v1"))


def test_ctx_snapshot_payload_validates_against_screen_context_schema() -> None:
    frame = _load_fixture("ctx_snapshot_payment_decline")
    _assert_valid(frame["payload"], _load_schema("screen_context.v1"))


def test_ctx_delta_payload_validates_against_ctx_delta_schema() -> None:
    frame = _load_fixture("ctx_delta_dialog_dismissed")
    _assert_valid(frame["payload"], _load_schema("ctx_delta.v1"))


def test_ctx_event_payload_validates_against_app_event_schema() -> None:
    frame = _load_fixture("ctx_event_tap_dismiss")
    _assert_valid(frame["payload"], _load_schema("app_event.v1"))


def test_every_context_fixture_file_is_wired_into_this_suite() -> None:
    """Mirror of test_voice_protocol.py's registry-coverage test, scoped to
    this task's own protocol/fixtures/context/ subdirectory (see module
    docstring for why a subdirectory, not the flat fixtures dir)."""
    wired = {f"{name}.json" for name in _ALL_FIXTURES}
    on_disk = {p.name for p in _FIXTURES_DIR.glob("*.json")}
    assert on_disk == wired, f"unwired: {on_disk - wired}; missing: {wired - on_disk}"


# ---------------------------------------------------------------------------
# Vocabulary consistency: the two schemas that duplicate the 16-role list
# must not silently drift apart (see ctx_delta.v1.json's own description)
# ---------------------------------------------------------------------------


def _screen_context_component_roles(schema: dict[str, Any]) -> set[str]:
    return {
        schema["$defs"][name]["properties"]["role"]["const"]
        for name in _SCREEN_CONTEXT_COMPONENT_DEF_NAMES
    }


def test_component_role_vocabulary_matches_docs07_sixteen_roles() -> None:
    schema = _load_schema("screen_context.v1")
    assert _screen_context_component_roles(schema) == set(_CORE_ROLES)


def test_ctx_delta_role_vocabulary_matches_screen_context_component_roles() -> None:
    """Guards the duplication ctx_delta.v1.json's own description flags:
    its changed_component/component_identity role enum must stay
    byte-identical to screen_context.v1.json's sixteen role consts, or the
    two schemas have silently drifted apart -- exactly the risk a protocol
    freeze exists to prevent."""
    screen_context_roles = _screen_context_component_roles(_load_schema("screen_context.v1"))
    delta_schema = _load_schema("ctx_delta.v1")
    changed_roles = set(delta_schema["$defs"]["changed_component"]["properties"]["role"]["enum"])
    identity_roles = set(delta_schema["$defs"]["component_identity"]["properties"]["role"]["enum"])
    assert changed_roles == screen_context_roles
    assert identity_roles == screen_context_roles


def test_ctx_delta_last_action_and_last_api_shapes_match_screen_context() -> None:
    """Review fix (T1 freeze review): ctx_delta.v1.json's own description
    flags $defs.last_action/$defs.last_api as duplicated locally from
    screen_context.v1.json because the walker only resolves local $ref --
    the same drift risk the role-vocabulary test above guards for the
    role enum, but nothing guarded these two duplicated shapes. Asserts
    both schemas' properties AND required lists stay byte-identical for
    each $def, so a future edit to one (e.g. a new last_action field)
    cannot silently diverge from the other."""
    screen_context_defs = _load_schema("screen_context.v1")["$defs"]
    delta_defs = _load_schema("ctx_delta.v1")["$defs"]
    for def_name in ("last_action", "last_api"):
        assert delta_defs[def_name]["properties"] == screen_context_defs[def_name]["properties"]
        assert set(delta_defs[def_name]["required"]) == set(
            screen_context_defs[def_name]["required"]
        )


def test_app_event_type_enum_has_exactly_five_closed_members() -> None:
    """docs/08 §2.1: 'The taxonomy is intentionally closed.'"""
    schema = _load_schema("app_event.v1")
    assert set(schema["properties"]["type"]["enum"]) == set(_APP_EVENT_FIXTURES.values())


# ---------------------------------------------------------------------------
# Negative: the schemas actually reject malformed input
# ---------------------------------------------------------------------------


def test_rejects_app_event_missing_ts() -> None:
    schema = _load_schema("app_event.v1")
    missing_ts = {"type": "tap", "name": "Pay Now", "screen": "PaymentScreen"}
    assert _errors(missing_ts, schema, schema.get("$defs", {}), "$")


def test_rejects_app_event_unknown_type() -> None:
    """docs/08 §2.1: a new event type is a protocol version bump, not
    something an existing v1 consumer must accept."""
    schema = _load_schema("app_event.v1")
    unknown_type = {"type": "scroll", "name": "settlements_list", "ts": 1784536500000}
    assert _errors(unknown_type, schema, schema.get("$defs", {}), "$")


def test_rejects_app_event_input_with_wrong_value_type() -> None:
    schema = _load_schema("app_event.v1")
    valid = _load_fixture("app_event_input")
    wrong_type = {**valid, "value": 245}
    assert _errors(wrong_type, schema, schema.get("$defs", {}), "$")


def test_rejects_screen_context_component_with_unknown_role() -> None:
    schema = _load_schema("screen_context.v1")
    valid = _load_fixture("screen_context_payment_decline")
    unknown_role = {**valid, "components": [{"role": "carousel", "label": "x"}]}
    assert _errors(unknown_role, schema, schema.get("$defs", {}), "$")


def test_rejects_screen_context_unsupported_version() -> None:
    schema = _load_schema("screen_context.v1")
    valid = _load_fixture("screen_context_payment_decline")
    wrong_version = {**valid, "v": "screen_context/v2"}
    assert _errors(wrong_version, schema, schema.get("$defs", {}), "$")


def test_rejects_screen_context_with_more_than_twenty_components() -> None:
    """docs/07 §3, §7: hard cap of 20 components."""
    schema = _load_schema("screen_context.v1")
    dense = _load_fixture("screen_context_settlements_dense")
    assert len(dense["components"]) == 20, "fixture should already sit exactly at the cap"
    over_cap = {**dense, "components": [*dense["components"], {"role": "image", "label": "21st"}]}
    assert _errors(over_cap, schema, schema.get("$defs", {}), "$")


def test_rejects_screen_context_component_label_over_120_chars() -> None:
    """docs/07 §5 rule 6: values normalized and length-capped at 120 chars."""
    schema = _load_schema("screen_context.v1")
    valid = _load_fixture("screen_context_payment_decline")
    too_long = {**valid, "components": [{"role": "image", "label": "x" * 121}]}
    assert _errors(too_long, schema, schema.get("$defs", {}), "$")


def test_rejects_ctx_delta_missing_base_seq() -> None:
    schema = _load_schema("ctx_delta.v1")
    valid = _load_fixture("ctx_delta_dialog_dismissed")["payload"]
    missing_base_seq = {k: v for k, v in valid.items() if k != "base_seq"}
    assert _errors(missing_base_seq, schema, schema.get("$defs", {}), "$")


def test_rejects_ctx_delta_changed_component_without_role() -> None:
    schema = _load_schema("ctx_delta.v1")
    valid = _load_fixture("ctx_delta_dialog_dismissed")["payload"]
    no_role = {**valid, "changed": [{"label": "no role here"}]}
    assert _errors(no_role, schema, schema.get("$defs", {}), "$")


def test_rejects_bool_where_ctx_envelope_version_const_is_an_int() -> None:
    """Regression for the walker's bool/int type-confusion, reusing this
    task's own fixtures against the existing (unmodified)
    data_channel_envelope.v1.json -- Python's `True == 1` must not let a
    boolean satisfy an integer `const`."""
    schema = _load_schema("data_channel_envelope.v1")
    valid = _load_fixture("ctx_snapshot_payment_decline")
    wrong_type = {**valid, "v": True}
    assert _errors(wrong_type, schema, schema.get("$defs", {}), "$")


# ---------------------------------------------------------------------------
# Forward compatibility (docs/13 §9): unknown additive fields tolerated
# ---------------------------------------------------------------------------


def test_screen_context_tolerates_unknown_additive_top_level_field() -> None:
    """docs/13 §9: new response/context fields are additive within v1, and
    a receiver must ignore fields it does not recognize. screen_context.v1
    .json deliberately leaves additionalProperties unset for exactly this
    -- a client that already ships a new field must not fail validation on
    a server (or test) that doesn't know about it yet."""
    schema = _load_schema("screen_context.v1")
    valid = _load_fixture("screen_context_payment_decline")
    with_future_field = {**valid, "confidence": "high"}
    assert not _errors(with_future_field, schema, schema.get("$defs", {}), "$")


def test_app_event_tolerates_unknown_additive_field() -> None:
    schema = _load_schema("app_event.v1")
    valid = _load_fixture("app_event_dialog")
    with_future_field = {**valid, "source": "compose_dialog_hook"}
    assert not _errors(with_future_field, schema, schema.get("$defs", {}), "$")


def test_ctx_delta_tolerates_unknown_additive_field() -> None:
    schema = _load_schema("ctx_delta.v1")
    valid = _load_fixture("ctx_delta_dialog_dismissed")["payload"]
    with_future_field = {**valid, "reason": "user_dismissed"}
    assert not _errors(with_future_field, schema, schema.get("$defs", {}), "$")
