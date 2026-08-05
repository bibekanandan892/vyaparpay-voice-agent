"""Contract tests binding the provisioned Grafana dashboards to the code
that actually emits the telemetry they query.

WHY THIS FILE EXISTS. A Grafana panel whose query names a span or attribute
nothing emits does not fail. It renders an empty graph, which is visually
identical to a healthy idle system — so the dashboard keeps looking fine
while it silently answers nothing. This project has already been bitten by
that exact shape once (the UiTreeCollector outage: a default-on filter
dropping data upstream of where anyone was looking). These tests make a
rename or a deleted call site fail loudly in CI instead.

The checks are deliberately grounded in RUNTIME/STATIC FACTS about the
code, never in a second hand-maintained list:

- span names come from the constants in `app.obs.tracing`, and the
  `tool.exec.<name>` prefix is obtained by actually opening a span through
  `tool_span()` and reading the name back off the exporter — so changing
  the f-string at tracing.py:190 breaks this, not just changing a constant;
- attribute keys are recovered by scanning `app/` for real
  `safe_set_attribute(span, "key", ...)` call sites. Note this is a
  STRICTER bar than the `_SAFE_SPAN_ATTRIBUTE_KEYS` allowlist: `cache_hit`
  is allowlisted but deliberately never set (context_builder.py:185), so a
  panel querying it would be permanently empty. Testing against the
  allowlist alone would wave that through;
- SQL column references are checked against the `CallCost` ORM model;
- datasource UIDs are checked against the provisioning YAML that defines
  them, and the dashboard provider's path against the compose mount.

There is one thing these tests CANNOT do: prove a query returns data from a
real Tempo. No Docker daemon was available when they were written. See
backend/DASHBOARDS.md, "What is not verified", for the exact list.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.util._once import Once

from app.config import Settings
from app.models.orm import CallCost
from app.obs.tracing import (
    _SAFE_SPAN_ATTRIBUTE_KEYS,
    SPAN_CONTEXT_BUILD,
    SPAN_LLM_TOTAL,
    SPAN_LLM_TTFT,
    SPAN_STT_FINAL,
    SPAN_TTS_FIRST_BYTE,
    SPAN_TURN,
    setup_observability,
    tool_span,
)

# backend/tests/obs/test_dashboards.py -> parents[2] == backend/, [3] == repo root
_BACKEND = Path(__file__).resolve().parents[2]
_REPO = Path(__file__).resolve().parents[3]
_APP = _BACKEND / "app"
_GRAFANA = _REPO / "infra" / "docker" / "grafana"
_DASHBOARD_DIR = _GRAFANA / "dashboards"
_DATASOURCE_DIR = _GRAFANA / "provisioning" / "datasources"
_PROVIDER_FILE = _GRAFANA / "provisioning" / "dashboards" / "dashboards.yaml"
_TEMPO_CONFIG = _REPO / "infra" / "docker" / "tempo" / "tempo.yaml"
_COMPOSE = _REPO / "docker-compose.yml"

# `safe_set_attribute(<span var>, "<key>", ...)` — the allowlisted path from
# app/obs/tracing.py, and the source of every attribute these boards query.
#
# It is NOT the only way a span attribute is set in this codebase:
# app/api/middleware.py:114-118 calls plain `span.set_attribute` for
# `http.method` / `http.target` / `http.status_code`, with a documented
# rationale (standard OTel semantic-convention keys, outside the allowlist's
# business-data domain). So this scan deliberately UNDER-reports. That
# direction is safe: a key it misses can only make a test false-FAIL —
# demanding a panel be removed — never false-pass a panel querying something
# nothing emits, which is the failure this file exists to prevent. If a
# board ever needs an `http.*` attribute, widen the scan rather than
# loosening the assertion.
_EMIT_CALL = re.compile(r"safe_set_attribute\(\s*[A-Za-z_][A-Za-z0-9_]*\s*,\s*\"([^\"]+)\"")

# TraceQL surface. `span:name` is Tempo 2.9's scoped intrinsic form; the
# negative lookahead keeps the `=` pattern from also swallowing `=~`.
_TRACEQL_NAME_EQ = re.compile(r"span:name\s*=(?!~)\s*\"([^\"]+)\"")
_TRACEQL_NAME_RE = re.compile(r"span:name\s*=~\s*\"([^\"]+)\"")
_TRACEQL_ATTR = re.compile(r"span\.([A-Za-z_][A-Za-z0-9_.]*)")
# The legacy unscoped intrinsic (`{ name = "turn" }`) still parses in Tempo
# 2.9 but is ambiguous with an attribute literally called `name`; this
# catches it so the boards stay on one convention.
_TRACEQL_BARE_NAME = re.compile(r"(?<![:.\w])name\s*=~?\s*\"")

# SQL column references, covering every shape the boards actually use:
# `col::float8`, `$__timeFilter(col)`, and `sum(col)` / `avg(col)`.
_SQL_CAST = re.compile(r"\b([a-z_]{3,})\s*::float8")
_SQL_TIMEFILTER = re.compile(r"\$__timeFilter\(\s*([a-z_]+)\s*\)")
_SQL_AGG = re.compile(r"\b(?:sum|avg|max|min)\(\s*([a-z_]+)\s*\)")


def _reset_tracer_provider() -> None:
    """Same set-once dance tests/obs/test_tracing.py documents: resetting
    `_TRACER_PROVIDER` alone is not enough, the `Once()` guard must be
    replaced too or `setup_observability` silently configures nothing."""
    otel_trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]
    otel_trace._TRACER_PROVIDER_SET_ONCE = Once()  # type: ignore[attr-defined]


def _emitted_attribute_keys() -> dict[str, list[str]]:
    """Every span-attribute key `app/` really emits, mapped to its
    `path:line` call sites. Static scan, so it needs no running app."""
    found: dict[str, list[str]] = {}
    for py in sorted(_APP.rglob("*.py")):
        text = py.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            for match in _EMIT_CALL.finditer(line):
                site = f"{py.relative_to(_BACKEND).as_posix()}:{lineno}"
                found.setdefault(match.group(1), []).append(site)
    return found


def _real_tool_span_name(settings: Settings) -> str:
    """Open a real `tool.exec.<name>` span and read the name back off an
    in-memory exporter. Asserting against this — rather than against a
    hardcoded "tool.exec." literal — is what makes a change to the f-string
    at app/obs/tracing.py:190 fail here."""
    _reset_tracer_provider()
    exporter = InMemorySpanExporter()
    setup_observability(settings, exporter=exporter)
    with tool_span("dashboard_probe"):
        pass
    otel_trace.get_tracer_provider().force_flush()  # type: ignore[attr-defined]
    spans = exporter.get_finished_spans()
    assert len(spans) == 1, f"expected exactly one probe span, got {len(spans)}"
    _reset_tracer_provider()
    return spans[0].name


def _dashboards() -> list[tuple[str, dict[str, Any]]]:
    files = sorted(_DASHBOARD_DIR.glob("*.json"))
    assert files, f"no dashboard JSON found under {_DASHBOARD_DIR}"
    return [(f.name, json.loads(f.read_text(encoding="utf-8"))) for f in files]


def _iter_targets(dashboard: dict[str, Any]):
    """Yield (panel_title, target) for every query on the board, including
    targets nested inside collapsed rows."""
    def walk(panels: list[dict[str, Any]]):
        for panel in panels:
            for target in panel.get("targets", []) or []:
                yield panel.get("title", "<untitled>"), target
            yield from walk(panel.get("panels", []) or [])

    yield from walk(dashboard.get("panels", []) or [])


def _traceql_queries() -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for name, dashboard in _dashboards():
        for title, target in _iter_targets(dashboard):
            query = target.get("query")
            if query:
                out.append((name, title, query))
    assert out, "no TraceQL queries found in any dashboard"
    return out


def _sql_queries() -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for name, dashboard in _dashboards():
        for title, target in _iter_targets(dashboard):
            sql = target.get("rawSql")
            if sql:
                out.append((name, title, sql))
    assert out, "no SQL queries found in any dashboard"
    return out


# ----------------------------------------------------------------------
# Structural validity
# ----------------------------------------------------------------------


def test_every_dashboard_parses_and_carries_the_required_top_level_keys() -> None:
    for name, dashboard in _dashboards():
        for key in ("uid", "title", "panels", "schemaVersion"):
            assert key in dashboard, f"{name}: missing top-level {key!r}"
        # Grafana migrates older schemaVersions forward on load, so an older
        # value is safe; a non-int is not.
        assert isinstance(dashboard["schemaVersion"], int), f"{name}: schemaVersion must be an int"
        assert dashboard["panels"], f"{name}: has no panels"


def test_dashboard_uids_and_titles_are_unique() -> None:
    uids = [d["uid"] for _, d in _dashboards()]
    titles = [d["title"] for _, d in _dashboards()]
    assert len(uids) == len(set(uids)), f"duplicate dashboard uid: {uids}"
    assert len(titles) == len(set(titles)), f"duplicate dashboard title: {titles}"


def test_every_panel_declares_a_title_and_every_query_panel_a_description() -> None:
    """A panel with no description is a panel whose reference line nobody
    can reconstruct six months later — docs/04 §7.3's whole premise is that
    each panel is read against a canonical number."""
    for name, dashboard in _dashboards():
        for panel in dashboard["panels"]:
            assert panel.get("title") is not None, f"{name}: panel {panel.get('id')} has no title"
            if panel.get("targets"):
                description = panel.get("description", "")
                assert len(description) > 40, (
                    f"{name}: query panel {panel['title']!r} needs a description "
                    "explaining its source and reference line"
                )


# ----------------------------------------------------------------------
# The span-name contract
# ----------------------------------------------------------------------


def test_dashboard_span_names_match_the_canon_constants(settings: Settings) -> None:
    """Renaming any SPAN_* constant in app/obs/tracing.py, or the
    `tool.exec.` prefix, must break this."""
    known = {
        SPAN_TURN,
        SPAN_CONTEXT_BUILD,
        SPAN_LLM_TTFT,
        SPAN_LLM_TOTAL,
        SPAN_STT_FINAL,
        SPAN_TTS_FIRST_BYTE,
        _real_tool_span_name(settings),
    }

    seen_exact: set[str] = set()
    seen_regex: set[str] = set()
    for file_name, title, query in _traceql_queries():
        for span_name in _TRACEQL_NAME_EQ.findall(query):
            assert span_name in known, (
                f"{file_name} / {title!r}: queries span name {span_name!r}, which no "
                f"constant in app.obs.tracing emits. Known: {sorted(known)}"
            )
            seen_exact.add(span_name)
        for pattern in _TRACEQL_NAME_RE.findall(query):
            compiled = re.compile(pattern)
            matched = {n for n in known if compiled.fullmatch(n)}
            assert matched, (
                f"{file_name} / {title!r}: regex span filter {pattern!r} matches none of "
                f"the emitted span names {sorted(known)} — the panel would be empty"
            )
            seen_regex |= matched

    # Guard the guard: if the extraction regexes silently stopped matching,
    # every assertion above would vacuously pass.
    assert seen_exact, "extracted no exact span-name filters — the TraceQL regex has drifted"
    assert seen_regex, "extracted no regex span-name filters — the TraceQL regex has drifted"


def test_dashboards_use_the_scoped_span_name_intrinsic() -> None:
    """`{ name = ... }` still parses in Tempo 2.9 but is ambiguous with an
    attribute called `name`; keep every board on `span:name`."""
    for file_name, title, query in _traceql_queries():
        assert not _TRACEQL_BARE_NAME.search(query), (
            f"{file_name} / {title!r}: uses the unscoped `name =` intrinsic; "
            "use `span:name =` instead"
        )


# ----------------------------------------------------------------------
# The span-attribute contract
# ----------------------------------------------------------------------


def test_dashboard_span_attributes_are_actually_emitted_somewhere_in_app() -> None:
    """The load-bearing test. An attribute that is merely allowlisted is
    NOT enough — it must have a real call site, or the panel is empty."""
    emitted = _emitted_attribute_keys()
    assert emitted, "static scan found no safe_set_attribute call sites at all"

    referenced: set[str] = set()
    for file_name, title, query in _traceql_queries():
        for attr in _TRACEQL_ATTR.findall(query):
            assert attr in emitted, (
                f"{file_name} / {title!r}: queries span attribute {attr!r}, which no "
                f"safe_set_attribute() call site in app/ emits — the panel would render "
                f"an empty graph rather than fail. Emitted keys: {sorted(emitted)}"
            )
            referenced.add(attr)

    assert referenced, "extracted no span attributes — the TraceQL attribute regex has drifted"


def test_dashboard_span_attributes_are_on_the_export_allowlist() -> None:
    """Belt and braces: anything a board reads must also be a key
    `safe_set_attribute` would permit, so a board can never document an
    attribute that only reached a span by bypassing the allowlist."""
    for file_name, title, query in _traceql_queries():
        for attr in _TRACEQL_ATTR.findall(query):
            assert attr in _SAFE_SPAN_ATTRIBUTE_KEYS, (
                f"{file_name} / {title!r}: span attribute {attr!r} is not in "
                "app.obs.tracing._SAFE_SPAN_ATTRIBUTE_KEYS"
            )


def test_allowlisted_but_unemitted_keys_are_not_queried_by_any_panel() -> None:
    """Documents the live example rather than asserting the gap is empty:
    `cache_hit` is allowlisted and deliberately never set
    (app/agent/context_builder.py:185). If a future batch starts emitting
    it this test still passes; what it forbids is a panel querying a key
    that is only ever allowlisted."""
    dead_keys = set(_SAFE_SPAN_ATTRIBUTE_KEYS) - set(_emitted_attribute_keys())
    queried = {
        attr for _, _, query in _traceql_queries() for attr in _TRACEQL_ATTR.findall(query)
    }
    assert not (dead_keys & queried), (
        f"panels query allowlisted-but-never-emitted attribute(s): {sorted(dead_keys & queried)}"
    )


# ----------------------------------------------------------------------
# The Postgres contract
# ----------------------------------------------------------------------


def test_dashboard_sql_columns_exist_on_the_call_cost_model() -> None:
    columns = {c.name for c in CallCost.__table__.columns}
    referenced: set[str] = set()
    for file_name, title, sql in _sql_queries():
        assert "call_costs" in sql, f"{file_name} / {title!r}: SQL does not read call_costs"
        found = (
            set(_SQL_CAST.findall(sql))
            | set(_SQL_TIMEFILTER.findall(sql))
            | set(_SQL_AGG.findall(sql))
        )
        for column in found:
            assert column in columns, (
                f"{file_name} / {title!r}: SQL references column {column!r}, which is not on "
                f"the CallCost model. Columns: {sorted(columns)}"
            )
        referenced |= found

    assert referenced, "extracted no SQL column references — the SQL regexes have drifted"


def test_dashboard_sql_is_read_only() -> None:
    """The Grafana Postgres datasource connects as the dev superuser, so
    read-only cannot be enforced by a GRANT here — it is enforced by the
    panels being SELECTs, which is what this asserts."""
    forbidden = ("insert ", "update ", "delete ", "drop ", "truncate ", "alter ", "create ")
    for file_name, title, sql in _sql_queries():
        lowered = sql.lower()
        assert lowered.lstrip().startswith(("select", "--")), (
            f"{file_name} / {title!r}: SQL does not start with SELECT"
        )
        for keyword in forbidden:
            assert keyword not in lowered, (
                f"{file_name} / {title!r}: SQL contains a write keyword {keyword.strip()!r}"
            )


# ----------------------------------------------------------------------
# Provisioning wiring
# ----------------------------------------------------------------------


def _provisioned_datasources() -> dict[str, str]:
    uids: dict[str, str] = {}
    files = sorted(_DATASOURCE_DIR.glob("*.yaml"))
    assert files, f"no datasource provisioning found under {_DATASOURCE_DIR}"
    for path in files:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
        for datasource in parsed.get("datasources", []):
            uids[datasource["uid"]] = datasource["type"]
    return uids


def test_every_panel_datasource_is_provisioned() -> None:
    """A panel pointing at a uid no provisioning file defines renders
    'Datasource not found' — which, on a board of otherwise-blank panels,
    is easy to read as 'no data yet'."""
    provisioned = _provisioned_datasources()
    checked = 0
    for name, dashboard in _dashboards():
        for title, target in _iter_targets(dashboard):
            datasource = target.get("datasource")
            assert datasource, f"{name} / {title!r}: target has no datasource"
            uid, type_ = datasource["uid"], datasource["type"]
            assert uid in provisioned, (
                f"{name} / {title!r}: datasource uid {uid!r} is not provisioned. "
                f"Provisioned: {sorted(provisioned)}"
            )
            assert provisioned[uid] == type_, (
                f"{name} / {title!r}: datasource {uid!r} is provisioned as "
                f"{provisioned[uid]!r} but the panel declares {type_!r}"
            )
            checked += 1
    assert checked, "no panel datasources were checked"


def test_dashboard_provider_path_matches_the_compose_mount() -> None:
    """The provider's `options.path` is a path INSIDE the container; the
    JSON lives on the host. If the compose mount target and this path drift
    apart, Grafana provisions an empty folder and every board silently
    disappears."""
    provider = yaml.safe_load(_PROVIDER_FILE.read_text(encoding="utf-8"))
    container_path = provider["providers"][0]["options"]["path"]

    compose = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
    mounts = compose["services"]["grafana"]["volumes"]
    targets = {m.split(":")[1] for m in mounts}
    assert container_path in targets, (
        f"dashboard provider path {container_path!r} is not a grafana volume mount target; "
        f"mounts are {sorted(targets)}"
    )

    # ...and the host side of that mount must be where the JSON actually is.
    host_side = next(m.split(":")[0] for m in mounts if m.split(":")[1] == container_path)
    resolved = (_REPO / host_side.lstrip("./")).resolve()
    assert resolved == _DASHBOARD_DIR.resolve(), (
        f"grafana mounts {resolved} at {container_path}, but the dashboards live in "
        f"{_DASHBOARD_DIR}"
    )


# ----------------------------------------------------------------------
# Tempo: the config that decides whether the metrics panels have any data
# ----------------------------------------------------------------------


def test_tempo_records_internal_spans_for_metrics_queries() -> None:
    """TRAP 1 from infra/docker/tempo/tempo.yaml. `filter_server_spans`
    defaults to TRUE, and the local-blocks filter then keeps only
    server-kind or root spans. Every span this project emits is
    SpanKind.INTERNAL, so with the default every child span — context.build,
    llm.ttft, llm.total, tool.exec.*, stt.final, tts.first_byte — is dropped
    at ingest and the whole latency waterfall silently returns no data."""
    tempo = yaml.safe_load(_TEMPO_CONFIG.read_text(encoding="utf-8"))
    local_blocks = tempo["metrics_generator"]["processor"]["local_blocks"]
    assert local_blocks["filter_server_spans"] is False, (
        "tempo.yaml must set metrics_generator.processor.local_blocks."
        "filter_server_spans: false, or the INTERNAL child spans this project "
        "emits are discarded before they reach a block"
    )
    assert tempo["metrics_generator"]["traces_storage"]["path"], (
        "metrics_generator.traces_storage.path is required — local-blocks cannot start "
        "without it"
    )


def test_tempo_enables_the_local_blocks_processor_in_overrides() -> None:
    """TRAP 2: the config block is `local_blocks` (underscore) but the
    overrides list value is `local-blocks` (hyphen). Configuring the
    processor without enabling it here leaves every TraceQL metrics panel
    empty."""
    tempo = yaml.safe_load(_TEMPO_CONFIG.read_text(encoding="utf-8"))
    processors = tempo["overrides"]["defaults"]["metrics_generator"]["processors"]
    assert "local-blocks" in processors, (
        f"overrides.defaults.metrics_generator.processors must contain 'local-blocks' "
        f"(hyphenated); found {processors}"
    )


def test_tempo_block_retention_covers_the_dashboard_time_windows() -> None:
    """A board defaulting to a 6h window over a 1h retention shows an empty
    graph for 5 of those 6 hours, which reads as an outage rather than as
    data ageing out."""
    tempo = yaml.safe_load(_TEMPO_CONFIG.read_text(encoding="utf-8"))
    retention = str(tempo["compactor"]["compaction"]["block_retention"])
    assert retention.endswith("h"), f"expected an hour-denominated retention, got {retention!r}"
    hours = int(retention.removesuffix("h"))

    widest = 0
    for name, dashboard in _dashboards():
        start = dashboard.get("time", {}).get("from", "now-1h")
        match = re.fullmatch(r"now-(\d+)h", start)
        assert match, f"{name}: unexpected default time range {start!r}"
        widest = max(widest, int(match.group(1)))

    assert hours >= widest, (
        f"tempo block_retention is {hours}h but a dashboard defaults to a {widest}h window"
    )


# ----------------------------------------------------------------------
# The docs/06 §4 latency budget the waterfall is read against
# ----------------------------------------------------------------------

# docs/06 §4's budget table, with the span mapping from §4.1's "OTel span /
# instrument" column. `None` means the stage is real but emits NO span, so
# it cannot appear in the stack:
#   - VAD endpoint  -> an `endpoint_ms` ATTRIBUTE on the outer `turn` span
#   - chunk/dispatch-> §4.1 marks it "span-adjacent"
#   - Opus/network  -> §4.1 says client-side WebRTC getStats, never a span
# docs/06 §4 is canon §7 and states it is "the only place [this] is derived",
# so this is a transcription of it, not a second source of truth. The first
# test below proves the transcription is faithful by reconciling it against
# the table's own stated totals.
_DOCS06_STAGES: dict[str, tuple[int, int, str | None]] = {
    "VAD endpoint detection": (250, 400, None),
    "STT finalization": (80, 150, SPAN_STT_FINAL),
    "Context assembly + prompt build": (15, 40, SPAN_CONTEXT_BUILD),
    "LLM time-to-first-token": (450, 900, SPAN_LLM_TTFT),
    "First sentence chunked -> TTS dispatch": (10, 20, None),
    "ElevenLabs Flash TTFB": (120, 250, SPAN_TTS_FIRST_BYTE),
    "Opus encode + network + jitter buffer": (75, 140, None),
}
_DOCS06_STATED_TOTAL_P50 = 1000
_DOCS06_STATED_TOTAL_P95 = 1900

_WATERFALL_PANEL = "Turn stage latency p95, stacked (the waterfall)"
# "665 ms p50 / 1,340 ms p95" wherever the expected stack is quoted.
_STATED_STACK = re.compile(r"\*\*([\d,]+) ms p50 / ([\d,]+) ms p95\*\*")


def _spanned_stage_budgets() -> tuple[set[str], int, int]:
    """The stages that DO emit a span, and their p50/p95 sums."""
    spanned = {s for _, _, s in _DOCS06_STAGES.values() if s is not None}
    p50 = sum(v[0] for v in _DOCS06_STAGES.values() if v[2] is not None)
    p95 = sum(v[1] for v in _DOCS06_STAGES.values() if v[2] is not None)
    return spanned, p50, p95


def _find_panel(title: str) -> dict[str, Any]:
    for _, dashboard in _dashboards():
        for panel in dashboard["panels"]:
            if panel.get("title") == title:
                return panel
    raise AssertionError(f"no panel titled {title!r} on any board")


def test_docs06_budget_transcription_reconciles_to_its_stated_totals() -> None:
    """Guards the transcription above, so the tests that depend on it are
    not building on a mistyped number."""
    assert sum(v[0] for v in _DOCS06_STAGES.values()) == _DOCS06_STATED_TOTAL_P50
    assert sum(v[1] for v in _DOCS06_STAGES.values()) == _DOCS06_STATED_TOTAL_P95


def test_waterfall_stacks_exactly_the_stages_that_emit_a_span() -> None:
    """A stacked panel is only meaningful if its bars partition the thing
    being measured. Adding a span with no docs/06 budget row — `llm.total`
    is the tempting one, since it wraps the whole generation and by design
    runs past the 'agent audio starts' boundary the budget ends at — makes
    the stack both double-count (it overlaps `tts.first_byte`) and measure
    a longer window than the budget it is compared against."""
    panel = _find_panel(_WATERFALL_PANEL)
    stage_target = panel["targets"][0]
    patterns = _TRACEQL_NAME_RE.findall(stage_target["query"])
    assert len(patterns) == 1, f"expected one span-name regex, got {patterns}"

    stacked = {branch.replace("\\.", ".") for branch in patterns[0].split("|")}
    spanned, _, _ = _spanned_stage_budgets()
    assert stacked == spanned, (
        f"the waterfall stacks {sorted(stacked)} but docs/06 §4.1 gives a budget row to "
        f"exactly {sorted(spanned)}. Every stacked span must map 1:1 to a budgeted stage."
    )


def test_documented_expected_stack_equals_the_budget_sum() -> None:
    """Both the on-board header and DASHBOARDS.md quote the height the
    stack should reach. If that number is not the sum of the stacked
    stages' budgets, a healthy system reads as a regression — the exact
    failure this board exists to prevent, inverted."""
    _, expected_p50, expected_p95 = _spanned_stage_budgets()

    sources = {
        "dashboard header panel": _find_panel("How to read this board")["options"]["content"],
        "DASHBOARDS.md": (_BACKEND / "DASHBOARDS.md").read_text(encoding="utf-8"),
    }
    for where, text in sources.items():
        matches = _STATED_STACK.findall(text)
        assert matches, f"{where}: no '<p50> ms p50 / <p95> ms p95' figure found"
        for stated_p50, stated_p95 in matches:
            assert int(stated_p50.replace(",", "")) == expected_p50, (
                f"{where}: states {stated_p50} ms p50, but the stacked stages sum to "
                f"{expected_p50} ms"
            )
            assert int(stated_p95.replace(",", "")) == expected_p95, (
                f"{where}: states {stated_p95} ms p95, but the stacked stages sum to "
                f"{expected_p95} ms"
            )


def test_documented_unspanned_stage_count_is_right() -> None:
    """The header says how many budgeted stages emit no span. Getting that
    count wrong is how the arithmetic above drifts in the first place."""
    unspanned = sum(1 for v in _DOCS06_STAGES.values() if v[2] is None)
    content = _find_panel("How to read this board")["options"]["content"]
    assert f"{'THREE' if unspanned == 3 else unspanned} of the seven" in content, (
        f"{unspanned} of the seven budgeted stages emit no span; the header panel does not "
        "say so"
    )


@pytest.mark.parametrize("required", ["tempo", "grafana"])
def test_obs_profile_services_still_exist(required: str) -> None:
    """The boards are worthless if the profile that serves them is renamed
    out from under them."""
    compose = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
    assert required in compose["services"], f"compose service {required!r} is gone"
    assert "obs" in compose["services"][required]["profiles"], (
        f"compose service {required!r} left the 'obs' profile"
    )
