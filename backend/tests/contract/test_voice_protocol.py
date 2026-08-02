"""Contract tests for the Phase-3 voice wire protocol (docs/13 §2.1, §4, §6).

Two layers, mirroring tests/tools/test_protocol_schemas.py's role for the
tool contracts:

**Layer 1 -- fixture ↔ schema (standalone, always runs).** Every fixture in
`protocol/fixtures/` must validate against its hand-written schema in
`protocol/schemas/`. `jsonschema` is deliberately NOT used: it is not a
backend dependency (checked `backend/pyproject.toml`; adding a dependency
is another task's call), so validation is a hand-rolled structural walker
in the house style of tests/tools/test_protocol_schemas.py, implementing
exactly the keyword subset the `protocol/schemas/*.json` files use:
`$ref` (local `#/$defs/…` only), `type` (string or list, with the JSON
Schema rules that `integer` satisfies `number`, bools are not integers,
and object/array keywords are ignored for non-object/non-array
instances), `const`, `enum`, `pattern` (re.search, per spec), `minimum`,
`properties`/`required`/`additionalProperties`, `items`/`minItems`,
`oneOf`, and `anyOf`. `format` is annotation-only, exactly as the
`jsonschema` library also treats it by default.

**Layer 2 -- fixture ↔ Pydantic round-trip (skips until the sibling branch
lands).** Each fixture must parse with the Pydantic models from
`app.domain.voice` and re-serialize byte-equivalently (compared on
canonical JSON bytes: sorted keys, tight separators, NFC-identical
values -- key order and whitespace are not wire-meaningful, everything
else is). Serialization uses `model_dump(mode="json",
exclude_unset=True)` so that optional-and-absent fields (a STUN entry's
missing `username`) stay absent while explicitly-null fields (an
end-of-candidates `"candidate": null`) stay null -- the only dump mode
that is a true inverse of parsing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

_PROTOCOL_DIR = Path(__file__).resolve().parents[3] / "protocol"
_SCHEMAS_DIR = _PROTOCOL_DIR / "schemas"
_FIXTURES_DIR = _PROTOCOL_DIR / "fixtures"

# fixture basename -> the envelope `type` it must carry (docs/13 §6 examples).
_SIGNALING_FIXTURES: dict[str, str] = {
    "signaling_offer": "offer",
    "signaling_answer": "answer",
    "signaling_ice": "ice",
    "signaling_bye": "bye",
    "signaling_error": "error",
    "signaling_ping": "ping",
    "signaling_pong": "pong",
}

# Only the server -> client types have fixtures: Phase 3 never sends ctx.*
# (docs/13 §4 -- context capture is Phase 4; the schema still defines them
# so the v1 envelope is complete). ctx.* fixtures land with Phase 4 capture.
_DATA_CHANNEL_FIXTURES: dict[str, str] = {
    "data_channel_transcript_partial": "transcript.partial",
    "data_channel_transcript_final": "transcript.final",
    "data_channel_agent_state": "agent.state",
}

_SESSION_REQUEST_FIXTURES = ["session_create_request", "session_create_request_phase3"]
_SESSION_RESPONSE_FIXTURES = ["session_create_response"]


def _load_fixture(name: str) -> Any:
    return json.loads((_FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _load_schema(name: str) -> dict[str, Any]:
    return json.loads((_SCHEMAS_DIR / f"{name}.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Layer 1: hand-rolled structural validation
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


def _check_scalars(value: Any, schema: dict[str, Any], path: str) -> list[str]:
    errs: list[str] = []
    if "const" in schema and value != schema["const"]:
        errs.append(f"{path}: {value!r} != const {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: {value!r} not in enum {schema['enum']!r}")
    if "pattern" in schema and isinstance(value, str) and not re.search(schema["pattern"], value):
        errs.append(f"{path}: {value!r} does not match pattern {schema['pattern']!r}")
    is_number = isinstance(value, int | float) and not isinstance(value, bool)
    if "minimum" in schema and is_number and value < schema["minimum"]:
        errs.append(f"{path}: {value!r} < minimum {schema['minimum']!r}")
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


@pytest.mark.parametrize(("fixture", "expected_type"), sorted(_SIGNALING_FIXTURES.items()))
def test_signaling_fixture_validates(fixture: str, expected_type: str) -> None:
    frame = _load_fixture(fixture)
    assert frame["type"] == expected_type
    _assert_valid(frame, _load_schema("signaling_message.v1"))


@pytest.mark.parametrize(("fixture", "expected_type"), sorted(_DATA_CHANNEL_FIXTURES.items()))
def test_data_channel_fixture_validates(fixture: str, expected_type: str) -> None:
    frame = _load_fixture(fixture)
    assert frame["type"] == expected_type
    _assert_valid(frame, _load_schema("data_channel_envelope.v1"))


@pytest.mark.parametrize("fixture", _SESSION_REQUEST_FIXTURES)
def test_session_create_request_fixture_validates(fixture: str) -> None:
    _assert_valid(_load_fixture(fixture), _load_schema("session_create_request.v1"))


@pytest.mark.parametrize("fixture", _SESSION_RESPONSE_FIXTURES)
def test_session_create_response_fixture_validates(fixture: str) -> None:
    _assert_valid(_load_fixture(fixture), _load_schema("session_create_response.v1"))


def test_every_signaling_type_has_a_fixture() -> None:
    """docs/13 §6 defines seven frame types; each must stay pinned by a
    byte-true fixture -- nobody extends the enum without adding one."""
    schema = _load_schema("signaling_message.v1")
    assert set(_SIGNALING_FIXTURES.values()) == set(schema["properties"]["type"]["enum"])


def test_every_fixture_file_is_wired_into_this_suite() -> None:
    """Mirror of tests/tools' registry-coverage test: a fixture dropped into
    protocol/fixtures/ that no test validates is a contract nobody checks."""
    wired = {
        f"{name}.json"
        for name in (
            list(_SIGNALING_FIXTURES)
            + list(_DATA_CHANNEL_FIXTURES)
            + _SESSION_REQUEST_FIXTURES
            + _SESSION_RESPONSE_FIXTURES
        )
    }
    on_disk = {p.name for p in _FIXTURES_DIR.glob("*.json")}
    assert on_disk == wired, f"unwired: {on_disk - wired}; missing: {wired - on_disk}"


def test_rejects_unknown_signaling_type_and_bad_payload() -> None:
    """Negative check that the walker actually bites: an unknown `type` and
    a payload violating its per-type def must both fail."""
    schema = _load_schema("signaling_message.v1")
    unknown = {"v": 1, "type": "renegotiate", "payload": {}}
    assert _errors(unknown, schema, schema.get("$defs", {}), "$")
    bad_payload = {"v": 1, "type": "bye", "payload": {"reason": "boredom"}}
    assert _errors(bad_payload, schema, schema.get("$defs", {}), "$")


# ---------------------------------------------------------------------------
# Layer 2: Pydantic round-trip against the sibling branch's domain models
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def voice() -> Any:
    # =====================================================================
    # LOUD GUARD -- READ BEFORE "FIXING" A SKIP.
    #
    # `app.domain.voice` is written on the parallel branch
    # `feat/voice-domain-contract` with exactly these names: SignalMessage,
    # DataChannelMessage, TranscriptPartial, TranscriptFinal, AgentStateMsg,
    # IceServer, SessionCredentials. Until that branch lands, every test
    # below SKIPS -- that is expected and green on this branch.
    #
    # The moment backend/app/domain/voice.py exists, importorskip imports it
    # and these tests RUN. This skip must NEVER survive past that point: if
    # you see these tests skipping on a tree that contains
    # app/domain/voice.py, something renamed or broke the module and the
    # wire contract is UNVERIFIED -- fix the import, do not delete the
    # tests, do not widen the skip.
    # =====================================================================
    return pytest.importorskip("app.domain.voice")


def _canonical(obj: Any) -> bytes:
    """Byte-stable canonical form: key order and whitespace are not
    wire-meaningful; everything else (values, presence/absence of keys,
    explicit nulls, unicode) is compared byte-for-byte."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def _assert_round_trip(model_cls: Any, wire_obj: Any) -> None:
    """Parse -> re-serialize must be the identity on canonical JSON bytes.
    `exclude_unset=True` keeps absent optionals absent (STUN entries carry
    no username) while preserving explicitly-sent nulls (end-of-candidates
    `"candidate": null`)."""
    dumped = model_cls.model_validate(wire_obj).model_dump(mode="json", exclude_unset=True)
    assert _canonical(dumped) == _canonical(wire_obj), (
        f"{model_cls.__name__} round-trip not byte-equivalent:\n"
        f"  fixture: {_canonical(wire_obj).decode()}\n"
        f"  dumped:  {_canonical(dumped).decode()}"
    )


@pytest.mark.parametrize("fixture", sorted(_SIGNALING_FIXTURES))
def test_signal_message_round_trips(voice: Any, fixture: str) -> None:
    _assert_round_trip(voice.SignalMessage, _load_fixture(fixture))


@pytest.mark.parametrize("fixture", sorted(_DATA_CHANNEL_FIXTURES))
def test_data_channel_message_round_trips(voice: Any, fixture: str) -> None:
    _assert_round_trip(voice.DataChannelMessage, _load_fixture(fixture))


def test_transcript_partial_payload_round_trips(voice: Any) -> None:
    frame = _load_fixture("data_channel_transcript_partial")
    _assert_round_trip(voice.TranscriptPartial, frame["payload"])


def test_transcript_final_payload_round_trips(voice: Any) -> None:
    frame = _load_fixture("data_channel_transcript_final")
    _assert_round_trip(voice.TranscriptFinal, frame["payload"])


def test_agent_state_payload_round_trips(voice: Any) -> None:
    frame = _load_fixture("data_channel_agent_state")
    _assert_round_trip(voice.AgentStateMsg, frame["payload"])


def test_session_credentials_round_trip(voice: Any) -> None:
    """The §2.1 connect bundle -- `data` of the 201 envelope, including the
    epoch-prefixed TURN username pair from docs/13 §7."""
    bundle = _load_fixture("session_create_response")["data"]
    _assert_round_trip(voice.SessionCredentials, bundle)


def test_ice_server_entries_round_trip(voice: Any) -> None:
    """Each entry standalone: the credential-free STUN entry must not grow
    null username/credential keys on re-serialization, and the TURN entry
    must keep its HMAC pair verbatim (docs/13 §7)."""
    entries = _load_fixture("session_create_response")["data"]["ice_servers"]
    assert len(entries) == 2
    for entry in entries:
        _assert_round_trip(voice.IceServer, entry)
