"""structlog processor chain — JSON logs, correlated to the active OTel span.

Processor order is load-bearing (docs/04-backend-architecture.md §7.1):
merge `contextvars` first (so `request_id`/`session_id`/`turn_id` bound
elsewhere are on every line), then level and timestamp, then stamp the
active span's trace/span ids so a log line and its span are joinable in
both directions (docs/04 §7.4), then render.

`request_id` is bound by an ASGI middleware in a later batch (app/api);
`session_id`/`turn_id` are bound by `ConversationManager` when it opens a
turn (also a later batch). This module only wires the chain — nothing here
binds those values itself.

`redact_pii` (docs/04 §7.1's belt-and-suspenders re-scan) is deliberately
NOT wired here: it depends on the card/Aadhaar/PAN patterns that are
`SafetyLayer`'s job (canon §12), and `SafetyLayer` doesn't exist yet in
this batch. Whichever batch builds it should add that processor here,
**after** `format_exc_info` in the list below, not before — `format_exc_info`
renders exception tracebacks/messages into the `exception` field, and a
badly-written exception message or a third-party error string can embed a
raw value; `redact_pii` needs to see that rendered string to have any
chance of catching it, so it must run after, scanning `format_exc_info`'s
output rather than being bypassed by it.

Binding contextvars (`request_id` via ASGI middleware, `session_id`/
`turn_id` via `ConversationManager`) is a later batch's job, not this
module's — but whichever batch does it MUST clear/reset on every exit
path (success, exception, cancellation), e.g. `bind_contextvars(...)`
paired with `clear_contextvars()` in a `finally`, never only on the
happy path. A context that isn't reset can bleed a `request_id` (or,
once bound, a `session_id` that correlates to a real account) into an
unrelated later log line. `tests/obs/test_logging.py` demonstrates the
required pattern.
"""

from __future__ import annotations

import logging

import orjson
import structlog
from opentelemetry import trace
from structlog.typing import EventDict, FilteringBoundLogger

from app.config import Settings


def _add_otel_ids(logger: object, method_name: str, event_dict: EventDict) -> EventDict:
    """Stamp `trace_id`/`span_id` from the currently active OTel span onto
    the log event (docs/04 §7.1 processor #3, §7.4 trace-to-log
    correlation). A no-op when there's no active recording span — true for
    any log call outside a turn/tool span, which is fine, it just means the
    line has nothing to correlate against.
    """
    span_context = trace.get_current_span().get_span_context()
    if span_context.is_valid:
        event_dict["trace_id"] = format(span_context.trace_id, "032x")
        event_dict["span_id"] = format(span_context.span_id, "016x")
    return event_dict


def configure_logging(settings: Settings) -> None:
    """Configure the process-wide structlog processor chain once at
    startup (agent-api lifespan, a later batch). Safe to call more than
    once (e.g. once per test) — `structlog.configure` is last-writer-wins.
    """
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _add_otel_ids,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(serializer=orjson.dumps),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.BytesLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> FilteringBoundLogger:
    """Everyday accessor every module uses instead of importing `structlog`
    directly: `log = get_logger(__name__)`.
    """
    return structlog.get_logger(name)


__all__ = ["configure_logging", "get_logger"]
