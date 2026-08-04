"""JSON-Schema-subset validator against the frozen wire schemas
(`protocol/schemas/{screen_context,ctx_delta,app_event}.v1.json`,
docs/08-context-and-events.md §4.1 check (c)).

Judgment call: this deliberately reimplements the same hand-rolled
structural walker `backend/tests/contract/test_voice_protocol.py` and
`backend/tests/contract/test_context_protocol.py` each already duplicate
locally, rather than adding `jsonschema` as a dependency. That test file's
own module docstring states the reasoning explicitly: "`jsonschema` is
deliberately not a backend dependency" for this repo, and `pyproject.toml`
confirms it — no `jsonschema` anywhere in `[project.dependencies]` or the
`dev` extra. This module is the *first* copy of that walker to live in
`app/` rather than `tests/`: `SnapshotIngestor` needs it at runtime (check
(c)), which the two test-only copies can't provide. It supports the exact
keyword subset both contract-test walkers use — `$ref` (local `#/$defs/...`
only), `type`, `const`, `enum`, `pattern`, `minimum`, `maxLength`,
`properties`/`required`/`additionalProperties`, `items`/`minItems`/
`maxItems`, `oneOf`, `anyOf` — everything the three frozen v1 schemas this
task validates against actually use, no more.

A follow-up that points the two contract-test copies at this module instead
of their own local duplicates would be a reasonable de-duplication, but is
out of this task's scope: `test_voice_protocol.py` and
`test_context_protocol.py` are both explicitly out of this task's file
ownership (docs/08 T1's already-merged protocol freeze owns them), and
`test_context_protocol.py`'s own docstring already explains why it didn't
import a shared helper from a file it doesn't own either — the same
argument applies in reverse here.
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path
from typing import Any

_SCHEMAS_DIR = Path(__file__).resolve().parents[3] / "protocol" / "schemas"


@cache
def load_schema(name: str) -> dict[str, Any]:
    """Load and cache one `protocol/schemas/{name}.json` file. `name` is
    the bare schema name (e.g. `"screen_context.v1"`), matching
    `test_context_protocol.py`'s own `_load_schema` helper's argument
    shape."""
    result: dict[str, Any] = json.loads((_SCHEMAS_DIR / f"{name}.json").read_text(encoding="utf-8"))
    return result


def _type_name(value: Any) -> str:
    """JSON type of a decoded Python value. `bool` is checked first: it is
    an `int` subclass in Python but a distinct JSON type, and `True == 1`
    must never let a boolean satisfy an integer `const`/`enum`/`type`."""
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
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _json_equal(a: Any, b: Any) -> bool:
    a_type, b_type = _type_name(a), _type_name(b)
    numeric = {"integer", "number"}
    if a_type != b_type and not (a_type in numeric and b_type in numeric):
        return False
    return bool(a == b)


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
    if "maxLength" in schema and isinstance(value, str) and len(value) > schema["maxLength"]:
        errs.append(f"{path}: length {len(value)} > maxLength {schema['maxLength']!r}")
    return errs


def _check_object(
    value: dict[str, Any], schema: dict[str, Any], defs: dict[str, Any], path: str
) -> list[str]:
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


def _check_array(
    value: list[Any], schema: dict[str, Any], defs: dict[str, Any], path: str
) -> list[str]:
    errs: list[str] = []
    if "minItems" in schema and len(value) < schema["minItems"]:
        errs.append(f"{path}: {len(value)} items < minItems {schema['minItems']}")
    if "maxItems" in schema and len(value) > schema["maxItems"]:
        errs.append(f"{path}: {len(value)} items > maxItems {schema['maxItems']}")
    if "items" in schema:
        for i, item in enumerate(value):
            errs.extend(_errors(item, schema["items"], defs, f"{path}[{i}]"))
    return errs


def _errors(value: Any, schema: dict[str, Any], defs: dict[str, Any], path: str) -> list[str]:
    """All violations of `schema` by `value`, as `path: reason` strings.
    Same recursive shape as the two contract-test walkers (see module
    docstring) — kept byte-for-byte equivalent in behavior so a fixture
    that validates in the contract-test suite validates here too."""
    if "$ref" in schema:
        ref_name = schema["$ref"].rsplit("/", 1)[-1]
        if ref_name not in defs:
            return [f"{path}: dangling $ref {schema['$ref']!r}"]
        merged = {**defs[ref_name], **{k: v for k, v in schema.items() if k != "$ref"}}
        return _errors(value, merged, defs, path)

    errs: list[str] = []
    if "type" in schema:
        allowed = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        actual = _type_name(value)
        if actual not in allowed and not (actual == "integer" and "number" in allowed):
            # Wrong shape entirely — nested keyword errors would only add noise.
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


def validate(instance: Any, schema: dict[str, Any]) -> list[str]:
    """All schema-violation messages for `instance` against `schema` (a
    loaded `protocol/schemas/*.v1.json` document); empty when valid."""
    return _errors(instance, schema, schema.get("$defs", {}), "$")


def is_valid(instance: Any, schema: dict[str, Any]) -> bool:
    return not validate(instance, schema)


__all__ = ["is_valid", "load_schema", "validate"]
