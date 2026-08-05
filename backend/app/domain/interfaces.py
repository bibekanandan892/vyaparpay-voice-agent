"""Frozen Protocols every agent-loop component and provider implements —
the Phase-2 loop (context, prompt, LLM, tools, safety, cost) plus the
Phase-5 memory subsystem (`EmbeddingProvider`, `SemanticMemoryProto`).

Pinning these signatures up front is what lets a phase's batches build in
parallel against a shared contract instead of each inventing its own
shape. If a batch's implementation needs to deviate from a signature
here, that's a contract change — update this file first (and note why),
don't silently drift in the implementation.

Docs: docs/04-backend-architecture.md §4-5 (providers, repositories),
docs/05-agent-architecture.md §3 (agent-loop components),
docs/09-memory-architecture.md §6 (semantic retrieval contract),
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
    RETRIEVAL_FLOOR,
    RETRIEVAL_TOP_K,
    ContextBundle,
    Embedding,
    EndReason,
    LLMEvent,
    Message,
    ModelTier,
    PendingConfirm,
    RetrievedMemory,
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


class EmbeddingProvider(Protocol):
    """docs/02 §3, docs/04 §4 — the fourth provider protocol, same shape as
    `LLMProvider` above: one vendor file behind one interface, model id in
    config, so swapping the embeddings vendor is a bounded change. Batch 2
    writes `OpenAIEmbeddings` against this; this file has no implementation.

    **Batched by construction.** `embed` takes a list and returns a list —
    there is no single-text convenience overload, deliberately. The seed
    embedder pushes ~180 KB chunks through this in one pass, and a
    one-text-per-call shape would turn that into ~180 sequential HTTP
    round trips. A caller with one string passes a one-element list.

    **Positional alignment is part of the contract**: `result[i]` is the
    embedding of `texts[i]`, and `len(result) == len(texts)`. Providers
    that reorder or drop inputs (e.g. silently skipping an over-long or
    empty string) would break every caller that zips the result back
    against its own source rows — which is exactly what the seed embedder
    and the post-call embed stage both do.

    **Dimension**: every returned vector is `EMBEDDING_DIM` (1536) floats
    from `EMBEDDING_MODEL` (`text-embedding-3-small`), per docs/09 §6.1.
    That cannot be expressed in the annotation — `Embedding` is
    `tuple[float, ...]` — so it is not enforced here. `MemoryChunk`
    re-checks the width before a vector can be stored, and the
    `vector(1536)` column rejects it at the DB boundary; a provider
    returning 3072-dim vectors fails there, not at this call.

    Failure behavior is the caller's, not this protocol's: docs/09 §11
    says an embedding outage skips the RAG slot entirely (drop-rung 1) —
    advisory by design, no keyword-search fallback in the demo.
    """

    async def embed(self, texts: list[str]) -> list[Embedding]: ...


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


class SemanticMemoryProto(Protocol):
    """docs/05 §3.7, docs/09 §6 — the Business Knowledge retriever:
    pgvector cosine top-k over the seeded support KB plus *this merchant's*
    past-call summaries.

    Signature is docs/05 §3.7's verbatim, with one named return type: the
    doc writes `-> list[Chunk]` and no `Chunk` exists in the doc set, so
    `RetrievedMemory` (app/domain/types.py) pins it — see that class's
    contract note.

    ---
    **`principal` is required — that is the structural half of the
    security control, and only the structural half.** The other half is
    the WHERE clause in the implementation; see the end of this docstring
    before relying on this one.

    docs/09 §6.2 states the scoping rule as SQL: `kind = 'kb_article' OR
    user_id = :session_user`. In a fintech product a `call_summary` from
    another merchant must be unreachable **by construction**, not by every
    caller remembering to pass a filter — docs/14's threat table treats
    cross-tenant reach by an authenticated user as a top-row risk, and
    docs/14 §5 makes the same structural argument for tools ("no tool
    accepts `user_id`; the executor injects the authenticated principal").
    This protocol reuses that established pattern rather than inventing a
    second one: `principal` is the same `SessionUser` `ToolExecutor`
    injects, it has **no default**, and no parameter on this signature can
    widen the scope.

    **What would break if it were optional.** Give `principal` a
    `SessionUser | None = None` default and the predicate has to degrade
    to something that still returns rows for a `None` scope — in practice
    `kind = 'kb_article'` only (silently useless retrieval, a quiet
    quality regression nobody notices) or, the way it actually tends to
    get written, the `user_id` clause is dropped entirely and every
    merchant's private call summaries become retrievable by every other
    merchant. The leak would be invisible in tests written by a caller
    who passed a principal, and would surface as Asha reciting another
    shop's payment history. Required-ness means the compiler and the
    interpreter both reject the omission: mypy errors on the missing
    argument, and CPython raises `TypeError` at the call. Leaking then
    requires deliberately passing someone else's principal — a different,
    much louder bug than forgetting a keyword argument.

    **What this protocol does NOT enforce, stated plainly.** A Protocol
    constrains the *signature*, not the SQL. An implementation can accept
    `principal` and never reference it in its WHERE clause, and nothing
    here would notice. The invariant is only real once Batch 2's
    `SemanticMemory` puts the scoping predicate in the single function
    that issues the query (docs/09 §6.2: "it lives in the one function
    that issues the query") and proves it with a test that seeds two
    merchants' summaries and asserts one cannot retrieve the other's.
    This docstring is the contract; that test is the enforcement. Do not
    read the required parameter as more than it is.

    **Writes are not on this protocol.** Indexing (post-call embed +
    insert, KB seed) belongs to `SemanticRepo` (docs/04 §5), not here —
    this is the read side only, so a later batch adding a write path adds
    it there rather than widening a retrieval interface that the scoping
    argument above depends on staying narrow.

    ---
    **Three things handed to Batch 2, named so they are a handoff and not
    an omission:**

    1. **`SemanticRepo` as the sole query path.** The strongest available
       encoding is for `SemanticRepo` (docs/04 §5) to own `search()` and
       be the only code that queries `memory_chunks`, so an unscoped query
       has nowhere to live. That cannot be built here — the repo does not
       exist yet — so what Batch 1 ships instead is
       `app.models.orm.memory_scope(principal)`, the predicate as one
       composable, unit-tested clause. Compose it; do not re-derive it.

    2. **A conformance check on the concrete class.** A required
       `principal` on this Protocol does not stop an implementation from
       *adding* a defaulted `include_all_users`-style parameter — that is
       legal structural widening and mypy accepts it (verified). The
       contract-freeze test in tests/contract/ pins this signature only.
       Batch 2 should assert its own class exposes no scope-affecting
       parameter beyond these.

    3. **HNSW post-filtering can silently return nothing.** pgvector's ANN
       scan collects candidates by vector distance *first*, then the
       scoping predicate removes non-matching ones, and
       `memory_chunks.user_id` is unindexed. A merchant whose summaries
       all fall outside the top `ef_search` candidates gets zero of their
       own rows — a recall bug that looks like "no relevant memory". Fix
       it with `ef_search`, iterative index scans, or a partial index.
       **Fetching a larger top-k globally and filtering in Python is
       forbidden**: it pulls other merchants' summaries into application
       memory, which is precisely the exposure the predicate prevents.
       Moot at the demo's ~200 rows (the planner scans sequentially);
       real as soon as the corpus grows.
    """

    async def retrieve(
        self,
        query: str,
        principal: SessionUser,
        *,
        k: int = RETRIEVAL_TOP_K,
        floor: float = RETRIEVAL_FLOOR,
    ) -> list[RetrievedMemory]: ...


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
