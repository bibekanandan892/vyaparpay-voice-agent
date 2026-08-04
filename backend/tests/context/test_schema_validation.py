"""`app.context.schema_validation` — the production copy of the hand-rolled
JSON-Schema-subset walker (see that module's own docstring for why it's a
copy, not an import, of the contract-test suite's walker). These tests
confirm parity on the behaviors `SnapshotIngestor` actually depends on;
`tests/contract/test_context_protocol.py` already owns exhaustive coverage
of the frozen schemas/fixtures themselves.
"""

from __future__ import annotations

from app.context.schema_validation import is_valid, load_schema, validate
from tests.context.conftest import load_fixture


def test_load_schema_is_cached_and_returns_the_same_dict_shape() -> None:
    first = load_schema("screen_context.v1")
    second = load_schema("screen_context.v1")
    assert first == second
    assert first["name"] == "screen_context.v1"


def test_valid_screen_context_fixture_has_no_errors() -> None:
    instance = load_fixture("screen_context_payment_decline")
    assert validate(instance, load_schema("screen_context.v1")) == []
    assert is_valid(instance, load_schema("screen_context.v1"))


def test_screen_context_missing_required_field_is_invalid() -> None:
    instance = {**load_fixture("screen_context_payment_decline")}
    del instance["dirty_fields"]
    errors = validate(instance, load_schema("screen_context.v1"))
    assert errors
    assert any("dirty_fields" in e for e in errors)


def test_screen_context_component_with_unknown_role_is_invalid() -> None:
    instance = {
        **load_fixture("screen_context_payment_decline"),
        "components": [{"role": "carousel", "label": "nope"}],
    }
    assert not is_valid(instance, load_schema("screen_context.v1"))


def test_screen_context_oneof_resolves_each_of_the_sixteen_roles() -> None:
    """The `component` $def's `oneOf` must match exactly one branch for
    every role actually on the wire — a regression here would mean the
    walker's `$ref` resolution or `oneOf` counting is broken."""
    instance = load_fixture("screen_context_settlements_dense")
    assert is_valid(instance, load_schema("screen_context.v1"))
    assert {c["role"] for c in instance["components"]}  # sanity: not empty


def test_valid_ctx_delta_payload_has_no_errors() -> None:
    payload = load_fixture("ctx_delta_dialog_dismissed")["payload"]
    assert is_valid(payload, load_schema("ctx_delta.v1"))


def test_ctx_delta_missing_base_seq_is_invalid() -> None:
    payload = load_fixture("ctx_delta_dialog_dismissed")["payload"]
    missing = {k: v for k, v in payload.items() if k != "base_seq"}
    assert not is_valid(missing, load_schema("ctx_delta.v1"))


def test_ctx_delta_changed_component_missing_role_is_invalid() -> None:
    payload = {**load_fixture("ctx_delta_dialog_dismissed")["payload"]}
    payload["changed"] = [{"label": "no role"}]
    assert not is_valid(payload, load_schema("ctx_delta.v1"))


def test_ctx_delta_changed_component_needs_only_role_and_label() -> None:
    """The documented T1 looseness `SnapshotIngestor`'s module docstring
    (judgment call 3) is built around: a `dialog` missing `visible`
    passes `ctx_delta.v1`'s own shallow per-component check."""
    payload = {
        "base_seq": 1,
        "changed": [{"role": "dialog", "label": "Daily Limit Exceeded"}],
        "removed": [],
    }
    assert is_valid(payload, load_schema("ctx_delta.v1"))


def test_bool_does_not_satisfy_an_integer_const() -> None:
    """Regression for the walker's bool/int type-confusion (Python's
    `True == 1`) — mirrors `test_context_protocol.py`'s own regression
    test for the same hazard."""
    envelope = {**load_fixture("ctx_snapshot_payment_decline"), "v": True}
    assert not is_valid(envelope, load_schema("data_channel_envelope.v1"))
