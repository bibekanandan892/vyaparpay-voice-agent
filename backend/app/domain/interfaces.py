"""Frozen Protocols every Phase-2 agent-loop component implements.

Pinning these signatures up front is what lets Batches 2-4 build in
parallel against a shared contract instead of each inventing its own
shape. If a batch's implementation needs to deviate from a signature
here, that's a contract change — update this file first (and note why),
don't silently drift in the implementation.

Docs: docs/04-backend-architecture.md §4-5 (providers, repositories),
docs/05-agent-architecture.md §3 (agent-loop components),
docs/10-tool-calling.md §2, §8 (tool registry/executor pipeline).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, Protocol, TypeVar

from opentelemetry.trace import Span
from pydantic import BaseModel

from app.domain.types import (
    ContextBundle,
    EndReason,
    LLMEvent,
    Message,
    ModelTier,
    PendingConfirm,
    SafetyVerdict,
    Session,
    SessionUser,
    TaskKind,
    Tier,
    ToolCall,
    ToolResult,
    TurnCost,
)

T = TypeVar("T")


# --------------------------------------------------------------------------
# Data layer
# --------------------------------------------------------------------------


class Repository(Protocol[T]):
    """Base repository contract every app/data/repositories/*.py module
    implements for its aggregate (docs/04 §5). Repos with composite keys
    (e.g. LimitRepo on merchant_id+limit_type, ConversationRepo's turns on
    session_id+turn_no) add domain-specific methods beyond this minimum —
    this Protocol is the floor, not the ceiling."""

    async def get(self, id: str) -> T | None: ...
    async def add(self, entity: T) -> T: ...
    async def update(self, entity: T) -> T: ...


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------


class LLMProvider(Protocol):
    """docs/04 §4. `messages` is already the wire-shape list[dict]
    (Message.to_wire() applied) by the time it reaches this layer —
    OpenRouterLLM itself does no Message-domain-type handling."""

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        models: list[str],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[LLMEvent]: ...


# --------------------------------------------------------------------------
# Tool registry (docs/10 §8) — shared between app/tools/ (registers) and
# app/agent/tool_executor.py (consumes)
# --------------------------------------------------------------------------


class ToolHandler(Protocol):
    """The async function body a `@tool`-decorated module provides. Never
    receives a user_id in `args` — `principal` is executor-injected
    (docs/10 §1 invariant 3)."""

    async def __call__(self, principal: SessionUser, args: BaseModel) -> BaseModel: ...


@dataclass(frozen=True)
class RegisteredTool:
    """One entry in the tool registry, as the `@tool` decorator in
    app/tools/registry.py produces it."""

    name: str
    tier: Tier
    latency_class: Literal["read-single", "write"]
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: ToolHandler
    description: str


class ToolRegistry(Protocol):
    """Implemented by app/tools/registry.py; consumed by ToolExecutor
    (lookup/allowlist) and LLMRouter/ConversationManager (the `tools`
    array passed to LLMProvider.stream())."""

    def get(self, name: str) -> RegisteredTool | None: ...
    def all(self) -> list[RegisteredTool]: ...
    def to_openai_tools(self) -> list[dict[str, Any]]: ...


# --------------------------------------------------------------------------
# Agent-loop components (docs/05 §3)
# --------------------------------------------------------------------------


class ContextBuilderProto(Protocol):
    """docs/05 §3.3. Adds `current_utterance` (keyword-only) beyond the
    doc's bare `build(session) -> ContextBundle` sketch, since the bundle
    has a `current_utterance` field that has to come from somewhere —
    ConversationManager passes the text it just got from `on_stt_final`."""

    async def build(self, session: Session, *, current_utterance: str) -> ContextBundle: ...


class PromptBuilderProto(Protocol):
    """docs/05 §3.3 — signature verbatim from the doc."""

    def render(self, bundle: ContextBundle) -> list[Message]: ...


class LLMRouterProto(Protocol):
    """docs/05 §3.4. `route()` picks the tier for a task; `stream()` owns
    the two fiddly interface pins: adapting list[Message] to the wire
    dicts LLMProvider.stream() wants, and reassembling streamed
    tool_call fragments (TokenEvent.delta) into whole ToolCalls before
    yielding them to the caller.

    Contract note (task 4.3): reassembled whole ToolCalls are yielded as
    one `ToolCallsEvent`, added to the `LLMEvent` union in
    app/domain/types.py — the original `TokenEvent | UsageEvent` union
    had no shape for them (see ToolCallsEvent's docstring)."""

    def route(self, task: TaskKind) -> ModelTier: ...

    async def stream(
        self,
        messages: list[Message],
        *,
        tier: ModelTier,
        tools: list[dict[str, Any]] | None = None,
        ttft_deadline_s: float = 1.5,
    ) -> AsyncIterator[LLMEvent]: ...


class ToolExecutorProto(Protocol):
    """docs/05 §3.5, docs/10 §2. Extends the docs' abbreviated
    `execute(calls, principal)` sketch with the parameters the
    confirm-gate/idempotency mechanics actually need: `session_id` and
    `turn_no` (idempotency key = f"{session_id}:{tool}:{turn_no}",
    docs/10 §4.2) and `affirmed` (set by ConversationManager when
    SafetyLayer.classify_affirmation() read the prior user utterance as
    an explicit yes to a pending confirm-required proposal)."""

    async def execute(
        self,
        calls: list[ToolCall],
        *,
        principal: SessionUser,
        session_id: str,
        turn_no: int,
        affirmed: bool = False,
    ) -> list[ToolResult]: ...


class SafetyLayerProto(Protocol):
    """docs/05 §3.6 — signatures verbatim from the doc."""

    def fence_input(self, bundle: ContextBundle) -> ContextBundle: ...
    def screen_output(self, text: str, tool_results: list[ToolResult]) -> SafetyVerdict: ...
    def classify_affirmation(self, utterance: str, pending: PendingConfirm | None) -> bool: ...
    def authorize_tool(self, call: ToolCall, principal: SessionUser) -> bool: ...


class CostTrackerProto(Protocol):
    """docs/05 §3.8 — signatures verbatim from the doc."""

    def record_turn(self, usage: dict[str, Any], model: str, span: Span) -> TurnCost: ...
    def call_total(self) -> Decimal: ...
    def over_budget(self, cap_usd: Decimal = Decimal("1.00")) -> bool: ...
    async def finalize(self, session_id: str) -> None: ...


class SessionManagerProto(Protocol):
    """docs/05 §3.1. `attach` is a Phase-2 no-op stub (no voice-worker to
    attach to yet). `create`'s `screen_context`/`recent_events` are typed
    as raw dicts here, not the doc's `ScreenContext`/`AppEvent` models
    (docs/07/08) — those pipelines don't exist until Phase 3/4, and Phase
    2's SessionManager.create() is always called with `screen_context=None,
    recent_events=[]` from the CLI harness. Tighten these types when the
    real ScreenContext ingestion lands instead of guessing its shape now."""

    async def create(
        self,
        user_id: str,
        screen_context: dict[str, Any] | None,
        recent_events: list[dict[str, Any]],
    ) -> Session: ...
    async def attach(self, session_id: str) -> Session: ...
    async def heartbeat(self, session_id: str) -> None: ...
    async def end(self, session_id: str, reason: EndReason) -> None: ...


class ConversationManagerProto(Protocol):
    """docs/05 §3.2. Phase 2 has no audio, so `on_barge_in` is unused but
    kept in the contract for shape-stability into Phase 3."""

    async def on_stt_final(self, text: str) -> str:
        """Opens a turn, runs the critical path, returns Asha's finalized
        reply text for this turn (the CLI harness prints this)."""
        ...

    async def on_barge_in(self) -> None: ...
