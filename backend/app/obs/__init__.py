"""Observability public surface — structlog + OpenTelemetry wiring.

Downstream code (agent loop, API middleware, tool executor — all later
batches) imports from `app.obs` rather than reaching into
`app.obs.logging` / `app.obs.tracing` directly, so this module is the one
place the public surface is pinned. See logging.py and tracing.py for the
implementation and docs/04-backend-architecture.md §7 for the design.
"""

from __future__ import annotations

from app.obs.logging import configure_logging, get_logger
from app.obs.tracing import (
    SPAN_CONTEXT_BUILD,
    SPAN_LLM_TOTAL,
    SPAN_LLM_TTFT,
    SPAN_TURN,
    get_tracer,
    safe_set_attribute,
    setup_observability,
    tool_span,
)

__all__ = [
    "SPAN_CONTEXT_BUILD",
    "SPAN_LLM_TOTAL",
    "SPAN_LLM_TTFT",
    "SPAN_TURN",
    "configure_logging",
    "get_logger",
    "get_tracer",
    "safe_set_attribute",
    "setup_observability",
    "tool_span",
]
