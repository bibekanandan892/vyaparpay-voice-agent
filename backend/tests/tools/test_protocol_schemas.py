"""Round-trip contract test (docs/10-tool-calling.md §8, step 1): each
tool's Pydantic input/output models must agree with the hand-written
JSON Schema in `protocol/tools/*.json` on the fields that matter, even
though Pydantic's auto-generated schema differs cosmetically from the
docs' hand-written style -- e.g. a nullable field renders as
`anyOf: [{type: X}, {type: "null"}]` in Pydantic v2 vs. the doc's
`"type": ["string", "null"]`; a nested submodel (`limit_context`) is
emitted as `$defs` + `$ref` rather than inlined.

**Comparison method** (documented here per the task brief, since an
exact-match `==` is explicitly not appropriate):

- Property *key sets* must be identical between the Pydantic schema and
  the hand-written one -- catches an added, removed, or renamed field in
  either direction.
- Each shared property's *effective type set* must agree, where
  "effective type set" normalizes both Pydantic's `anyOf`-with-`null`
  idiom and the hand-written schema's `["string", "null"]` idiom (or a
  bare `"type": "string"`) down to the same `set[str]`.
- A `pattern` present on either side must be present and equal on both.
- An `enum`/`const` present on either side must be present and equal (as
  a set of values) on both.
- `required` is compared only where the hand-written schema actually
  states one -- docs/10 §3.1's own output schemas never state a
  `required` list (every output field there is implicitly required by
  the doc's own convention), so requiring `required`-list equality on
  outputs would fail against the doc text itself, not a real
  regression. Inputs do state `required`, and are checked. One output
  schema is the exception: `get_wallet_balance.json`'s output (the
  implementer's own addition, since docs/10 §3 only gives the compact
  catalog row for this tool, not a full schema) DOES declare a
  `required` list -- checked, unlike its two siblings.
- `additionalProperties`, when the hand-written schema states it
  (every Phase-2 input schema does, as `false` -- the schema-level
  backstop for invariant 3, docs/10 §1: no LLM-suppliable
  principal-identifying field), must match on both sides. Added after
  review (MEDIUM): a future edit that silently dropped `extra="forbid"`
  from an input model would have passed every check above unchanged,
  even though it degrades exactly the security property this suite
  exists to guard.
- `limit_context` (the one nested object in the Phase-2 catalog) is
  checked separately against `LimitContextOut.model_json_schema()`,
  since the generic property-set comparison above only reasons about
  one flat level.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.tools.get_payment_status import GetPaymentStatusIn, GetPaymentStatusOut, LimitContextOut
from app.tools.get_wallet_balance import GetWalletBalanceIn, GetWalletBalanceOut
from app.tools.request_limit_increase import RequestLimitIncreaseIn, RequestLimitIncreaseOut

_PROTOCOL_DIR = Path(__file__).resolve().parents[3] / "protocol" / "tools"


def _load(name: str) -> dict[str, Any]:
    return json.loads((_PROTOCOL_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _resolve(schema: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """Recursively inlines every `$ref` (Pydantic's rendering of a
    nested submodel) and descends into `anyOf` branches, so the rest of
    this test's comparisons never have to special-case either."""
    if "$ref" in schema:
        ref_name = schema["$ref"].rsplit("/", 1)[-1]
        resolved = defs.get(ref_name, {})
        merged = {**resolved, **{k: v for k, v in schema.items() if k != "$ref"}}
        return _resolve(merged, defs)
    if "anyOf" in schema:
        return {**schema, "anyOf": [_resolve(branch, defs) for branch in schema["anyOf"]]}
    return schema


def _effective_types(prop: dict[str, Any]) -> set[str]:
    """Normalizes Pydantic's `anyOf: [{type: X}, {type: "null"}]` and the
    hand-written schema's `"type": ["string", "null"]` (or a bare
    `"type": "string"`) down to the same `set[str]`."""
    if "anyOf" in prop:
        types: set[str] = set()
        for branch in prop["anyOf"]:
            types |= _effective_types(branch)
        return types
    raw = prop.get("type")
    if raw is None:
        return set()
    return {raw} if isinstance(raw, str) else set(raw)


def _effective_enum(prop: dict[str, Any]) -> set[Any] | None:
    """`None` means "this side states no enum/const constraint" --
    distinct from an empty set, so a caller can tell "not applicable"
    from "constrained to nothing"."""
    if "const" in prop:
        return {prop["const"]}
    if "enum" in prop:
        return set(prop["enum"])
    if "anyOf" in prop:
        values: set[Any] = set()
        found = False
        for branch in prop["anyOf"]:
            branch_enum = _effective_enum(branch)
            if branch_enum is not None:
                found = True
                values |= branch_enum
        return values if found else None
    return None


def _assert_property_compatible(
    name: str, pyd_prop: dict[str, Any], hw_prop: dict[str, Any]
) -> None:
    hw_types = _effective_types(hw_prop)
    if hw_types:
        assert _effective_types(pyd_prop) == hw_types, (
            f"{name}: type mismatch pydantic={_effective_types(pyd_prop)} protocol={hw_types}"
        )

    if "pattern" in hw_prop or "pattern" in pyd_prop:
        assert pyd_prop.get("pattern") == hw_prop.get("pattern"), f"{name}: pattern mismatch"

    hw_enum = _effective_enum(hw_prop)
    if hw_enum is not None:
        assert _effective_enum(pyd_prop) == hw_enum, f"{name}: enum mismatch"


def _assert_schema_compatible(
    pydantic_schema: dict[str, Any], hand_written: dict[str, Any], *, check_required: bool
) -> None:
    defs = pydantic_schema.get("$defs", {})
    pyd_props = {
        key: _resolve(value, defs) for key, value in pydantic_schema.get("properties", {}).items()
    }
    hw_props = hand_written.get("properties", {})

    assert set(pyd_props) == set(hw_props), (
        f"property set mismatch: pydantic={set(pyd_props)} protocol={set(hw_props)}"
    )
    for name, hw_prop in hw_props.items():
        _assert_property_compatible(name, pyd_props[name], hw_prop)

    if check_required and "required" in hand_written:
        assert set(pydantic_schema.get("required", [])) == set(hand_written["required"])

    if "additionalProperties" in hand_written:
        assert (
            pydantic_schema.get("additionalProperties") == hand_written["additionalProperties"]
        ), (
            f"additionalProperties mismatch: pydantic="
            f"{pydantic_schema.get('additionalProperties')!r} "
            f"protocol={hand_written['additionalProperties']!r}"
        )


# --------------------------------------------------------------------------
# get_wallet_balance
# --------------------------------------------------------------------------


def test_get_wallet_balance_input_round_trips() -> None:
    contract = _load("get_wallet_balance")
    _assert_schema_compatible(
        GetWalletBalanceIn.model_json_schema(), contract["input"], check_required=True
    )


def test_get_wallet_balance_output_round_trips() -> None:
    """`check_required=True` here, unlike the sibling output tests below
    (fixed after review, MEDIUM): `get_wallet_balance.json`'s output is
    the implementer's own addition (docs/10 §3 gives only the compact
    catalog row for this tool, not a full schema) and it DOES declare a
    `required` list -- the other two tools' output schemas are copied
    verbatim from doc prose that states none. Leaving this at `False`
    meant the one explicit constraint this file's own JSON adds was
    silently never verified."""
    contract = _load("get_wallet_balance")
    _assert_schema_compatible(
        GetWalletBalanceOut.model_json_schema(), contract["output"], check_required=True
    )


# --------------------------------------------------------------------------
# get_payment_status
# --------------------------------------------------------------------------


def test_get_payment_status_input_round_trips() -> None:
    contract = _load("get_payment_status")
    _assert_schema_compatible(
        GetPaymentStatusIn.model_json_schema(), contract["input"], check_required=True
    )


def test_get_payment_status_output_round_trips() -> None:
    contract = _load("get_payment_status")
    _assert_schema_compatible(
        GetPaymentStatusOut.model_json_schema(), contract["output"], check_required=False
    )


def test_get_payment_status_output_limit_context_round_trips() -> None:
    """`limit_context` is the one nested object in the Phase-2 catalog --
    verified separately against `LimitContextOut` since the generic
    helper above only reasons about one flat property level."""
    contract = _load("get_payment_status")
    hw_limit_context = contract["output"]["properties"]["limit_context"]
    _assert_schema_compatible(
        LimitContextOut.model_json_schema(), hw_limit_context, check_required=False
    )


# --------------------------------------------------------------------------
# request_limit_increase
# --------------------------------------------------------------------------


def test_request_limit_increase_input_round_trips() -> None:
    contract = _load("request_limit_increase")
    _assert_schema_compatible(
        RequestLimitIncreaseIn.model_json_schema(), contract["input"], check_required=True
    )


def test_request_limit_increase_output_round_trips() -> None:
    contract = _load("request_limit_increase")
    _assert_schema_compatible(
        RequestLimitIncreaseOut.model_json_schema(), contract["output"], check_required=False
    )


# --------------------------------------------------------------------------
# Sanity: registry <-> protocol/ coverage
# --------------------------------------------------------------------------


def test_every_registered_tool_has_a_protocol_schema_file() -> None:
    """docs/10 §8 step 1: nobody can add a tool to the registry without
    also adding its `protocol/tools/*.json` contract file."""
    import app.tools  # noqa: F401 -- import side effect: registers all 3 Phase-2 tools
    from app.tools.registry import registry

    for registered in registry.all():
        assert (_PROTOCOL_DIR / f"{registered.name}.json").exists(), (
            f"missing protocol/tools/{registered.name}.json"
        )
