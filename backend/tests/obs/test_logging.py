"""Unit tests for app.obs.logging — pure in-process, no collector needed.

These parse the actual emitted JSON line (via pytest's `capsysbinary` —
`structlog.BytesLoggerFactory()` writes to `sys.stdout.buffer`, which
`capsysbinary` captures) rather than only asserting "the call didn't
raise". Verified against real structlog/opentelemetry behavior, not just
read from the source, before being relied on here — an earlier version of
this suite only checked for no exception and would not have caught a
broken processor, a wrong key name, or a missing trace_id/span_id stamp.
"""

from __future__ import annotations

import json

import pytest
import structlog
from opentelemetry import trace as otel_trace
from opentelemetry.util._once import Once

from app.config import Settings
from app.obs.logging import configure_logging, get_logger
from app.obs.tracing import SPAN_TURN, get_tracer, setup_observability


def _settings(**overrides: object) -> Settings:
    """Minimal valid Settings for a test — only the three fields with no
    default (docs/04's fail-fast-on-missing-secrets rule) need supplying;
    kwargs win over any real .env, so this is hermetic."""
    defaults: dict[str, object] = {
        "jwt_secret": "test-secret",
        "database_url": "postgresql+asyncpg://user:pass@localhost/test",
        "openrouter_api_key": "test-key",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _reset_tracer_provider() -> None:
    """A couple of tests below open a real span to check trace/span id
    stamping (`_add_otel_ids`); reset both the cached provider and its
    `Once()` guard so `setup_observability` actually takes effect each
    time — see test_tracing.py's module docstring for why both matter."""
    otel_trace._TRACER_PROVIDER = None
    otel_trace._TRACER_PROVIDER_SET_ONCE = Once()
    yield
    otel_trace._TRACER_PROVIDER = None
    otel_trace._TRACER_PROVIDER_SET_ONCE = Once()


def _last_json_line(raw: bytes) -> dict[str, object]:
    return json.loads(raw.strip().splitlines()[-1])


def test_configure_logging_does_not_raise() -> None:
    configure_logging(_settings(log_level="DEBUG"))


def test_configure_logging_unknown_level_falls_back_to_info(capsysbinary) -> None:
    """`configure_logging` reads `settings.log_level`; an unrecognized
    string (e.g. a typo in an env var) must not raise — it degrades to
    INFO rather than crashing the process at startup, and an INFO-level
    call must actually make it through (proving the fallback landed on
    INFO or below, not on something stricter that would swallow it)."""
    configure_logging(_settings(log_level="NOT_A_LEVEL"))

    log = get_logger(__name__)
    log.info("test_event")

    line = _last_json_line(capsysbinary.readouterr().out)
    assert line["event"] == "test_event"
    assert line["level"] == "info"


def test_get_logger_info_emits_valid_json_with_expected_keys(capsysbinary) -> None:
    configure_logging(_settings())

    log = get_logger(__name__)
    log.info("test_event", foo="bar")

    line = _last_json_line(capsysbinary.readouterr().out)
    assert line["event"] == "test_event"
    assert line["foo"] == "bar"
    assert line["level"] == "info"
    assert "timestamp" in line


def test_bound_contextvars_appear_on_the_log_line(capsysbinary) -> None:
    """`merge_contextvars` (the processor this module is required to wire)
    — a value bound via structlog.contextvars flows onto the next log call
    without being passed explicitly. Later batches (API middleware,
    ConversationManager) rely on exactly this to attach request_id /
    session_id / turn_id to every line inside a request or turn."""
    configure_logging(_settings())
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id="req-123")

    try:
        log = get_logger(__name__)
        log.info("test_event")
    finally:
        structlog.contextvars.clear_contextvars()

    line = _last_json_line(capsysbinary.readouterr().out)
    assert line["request_id"] == "req-123"


def test_active_span_stamps_trace_and_span_id_on_the_log_line(capsysbinary) -> None:
    """`_add_otel_ids` (docs/04 §7.1 processor #3) — a log line emitted
    while a span is open must carry that span's trace_id/span_id, so the
    line and the span are joinable in both directions (docs/04 §7.4)."""
    configure_logging(_settings())
    setup_observability(_settings(otel_exporter_otlp_endpoint=None))

    tracer = get_tracer(__name__)
    with tracer.start_as_current_span(SPAN_TURN):
        get_logger(__name__).info("inside_span_event")

    line = _last_json_line(capsysbinary.readouterr().out)
    assert len(line["trace_id"]) == 32  # 128-bit trace id, hex
    assert len(line["span_id"]) == 16  # 64-bit span id, hex


def test_no_active_span_omits_trace_and_span_id(capsysbinary) -> None:
    """The converse of the test above — `_add_otel_ids` is a no-op outside
    a recording span, so a log line with nothing to correlate against
    shouldn't carry fabricated ids."""
    configure_logging(_settings())

    get_logger(__name__).info("no_span_event")

    line = _last_json_line(capsysbinary.readouterr().out)
    assert "trace_id" not in line
    assert "span_id" not in line
