"""Shared domain value types for the agent loop.

These are the interchange types every module in app/agent/, app/tools/,
app/memory/, and app/providers/ passes between each other — pinned once
here (per each phase's "contract freeze" batch: Phase 2 froze the turn
loop's shapes, Phase 5 added the memory subsystem's) so parallel work on
those packages doesn't each invent a slightly different ToolResult or
MemoryChunk shape.

All value objects are frozen pydantic models: never mutate one in place,
build a new instance instead (`model_copy(update={...})` when you need a
derived copy) — see ~/.claude/rules/ecc/common/coding-style.md.

Docs: docs/05-agent-architecture.md (turn lifecycle, component interfaces),
docs/10-tool-calling.md (tool wire shapes), docs/11-prompt-engineering.md
(prompt slots), docs/12-data-models.md (status enums mirror DB CHECKs).
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_FROZEN = ConfigDict(frozen=True)


# --------------------------------------------------------------------------
# Roles, states, enums — mirror DB CHECK constraints (docs/12) where one exists
# --------------------------------------------------------------------------


class Role(StrEnum):
    """Chat message role. Mirrors OpenAI-compatible `messages[].role`.

    NOT the same vocabulary as `conversation_turns.role` (docs/12 §4.2),
    which is the coarser `'user' | 'agent'` (who spoke this turn, not
    which chat-array role produced a given LLM-facing message). Do not
    write `Role.ASSISTANT.value` ("assistant") into `conversation_turns`
    — it violates that table's CHECK constraint. Map ASSISTANT/TOOL/SYSTEM
    -> "agent" and USER -> "user" at the persistence boundary instead.
    """

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class TaskKind(StrEnum):
    """What kind of work a turn needs — input to LLMRouter.route()."""

    DIALOGUE = "dialogue"
    UTILITY = "utility"


class ModelTier(StrEnum):
    """Which model tier LLMRouter.route() selected. Phase 2 only ever
    reaches DIALOGUE (Sonnet 5) on the hot path; UTILITY (Haiku 4.5) is
    wired for the classify_affirmation upgrade path (decision #5) but not
    exercised by any Phase-2 turn.
    """

    DIALOGUE = "dialogue"
    UTILITY = "utility"


class TurnState(StrEnum):
    """ConversationManager's per-turn state machine (docs/05 §3.2)."""

    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    SPEAKING = "speaking"


class SessionState(StrEnum):
    """Mirrors conversations.state CHECK (docs/12 §4.1)."""

    CREATED = "created"
    IN_CALL = "in_call"
    WRAP_UP = "wrap_up"
    ENDED = "ended"


class EndReason(StrEnum):
    """Why SessionManager.end() was called."""

    HANGUP = "hangup"
    ESCALATED = "escalated"
    TIMEOUT = "timeout"
    ERROR = "error"


class Tier(StrEnum):
    """Tool authorization tier (docs/10 §1). Phase 2 only registers READ and
    CONFIRM_REQUIRED tools; SENSITIVE/CONTROL exist so the enum doesn't need
    to change shape when block_card/reset_pin-style tools land later.
    """

    READ = "read"
    CONFIRM_REQUIRED = "confirm_required"
    SENSITIVE = "sensitive"
    CONTROL = "control"


class ToolInvocationStatus(StrEnum):
    """Mirrors tool_invocations.status CHECK (docs/12 §4.4)."""

    OK = "ok"
    ERROR = "error"
    DENIED = "denied"
    PENDING_CONFIRM = "pending_confirm"
    CANCELLED = "cancelled"


class Resolution(StrEnum):
    """Mirrors conversation_summaries.resolution CHECK (docs/09 §8,
    docs/12 §4.3) — how the call ended, as the post-call pipeline
    classified it."""

    RESOLVED = "resolved"
    ESCALATED = "escalated"
    PENDING = "pending"
    ABANDONED = "abandoned"


class MemoryKind(StrEnum):
    """Mirrors memory_chunks.kind CHECK (docs/09 §6.1, docs/12 §5) — the
    discriminator that lets one table and one HNSW index carry both the
    seeded support KB and this merchant's past-call summaries.

    It is also half of the retrieval scoping predicate (docs/09 §6.2):
    `kind = 'kb_article' OR user_id = :session_user`. KB_ARTICLE is the
    globally-readable kind; CALL_SUMMARY is the per-merchant kind that
    must never cross a tenant boundary.
    """

    KB_ARTICLE = "kb_article"
    CALL_SUMMARY = "call_summary"


# --------------------------------------------------------------------------
# Messages and tool calls
# --------------------------------------------------------------------------


class ToolCall(BaseModel):
    """One tool invocation the LLM asked for, reassembled from streamed
    OpenAI-style tool_call fragments (that reassembly is LLMRouter's job,
    app/agent/llm_router.py — see the plan's interface-pin note)."""

    model_config = _FROZEN

    id: str
    name: str
    arguments: dict[str, Any]


class Message(BaseModel):
    """One chat-array entry. `PromptBuilder.render()` produces a
    `list[Message]`; `LLMRouter` adapts that to the `list[dict]` shape
    `OpenRouterLLM.stream()` expects (the other half of the interface pin).
    """

    model_config = _FROZEN

    role: Role
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] | None = None  # assistant messages proposing calls
    tool_call_id: str | None = None  # tool-role messages: which call this answers
    name: str | None = None  # tool-role messages: the tool name

    def to_wire(self) -> dict[str, Any]:
        """OpenAI-compatible dict shape for the OpenRouter request body."""
        out: dict[str, Any] = {"role": self.role.value}
        if self.content is not None:
            out["content"] = self.content
        if self.tool_calls:
            # OpenAI-compatible wire format requires `arguments` as a
            # JSON-encoded *string*, never a nested object — ToolCall.arguments
            # stays dict[str, Any] as a domain value (that's what Pydantic
            # validation needs), this is the one place it gets serialized.
            out["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments, separators=(",", ":")),
                    },
                }
                for tc in self.tool_calls
            ]
        if self.tool_call_id is not None:
            out["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            out["name"] = self.name
        return out


class ToolResult(BaseModel):
    """Outcome of one tool invocation, in the exact wire shape docs/10 §2/§5
    specifies: `{"ok": true, "data": {...}}` on success, or `{"ok": false,
    "error": {...}}` / `{"ok": false, "gate": {...}}` on failure/hold.
    `status` mirrors the tool_invocations audit row written for this call.
    """

    model_config = _FROZEN

    tool_call_id: str
    tool_name: str
    ok: bool
    data: dict[str, Any] | None = None
    # {"type","code","fields"|"detail"|"elapsed_ms","retryable"} — "detail"/"fields"
    # must be pre-vetted, user/LLM-safe content only, never raw exception text or
    # DB error strings (this re-enters the conversation via to_llm_message() below
    # and is voiced/shown to the end user).
    error: dict[str, Any] | None = None
    # {"status":"pending_confirm","instruction":...,"action":{...}}
    gate: dict[str, Any] | None = None
    status: ToolInvocationStatus
    latency_ms: int
    idempotency_key: str | None = None

    def to_llm_message(self) -> Message:
        """Render this result as the `role: tool` message that re-enters
        the conversation (docs/11 §5). Compression/truncation to ≤120
        tokens is ToolExecutor's job before this is called, not this
        method's."""
        payload: dict[str, Any] = {"ok": self.ok}
        if self.data is not None:
            payload["data"] = self.data
        if self.error is not None:
            payload["error"] = self.error
        if self.gate is not None:
            payload["gate"] = self.gate

        return Message(
            role=Role.TOOL,
            content=json.dumps(payload, separators=(",", ":")),
            tool_call_id=self.tool_call_id,
            name=self.tool_name,
        )


# --------------------------------------------------------------------------
# Prompt / context assembly
# --------------------------------------------------------------------------


class ContextBundle(BaseModel):
    """The 9-slot assembly ContextBuilder produces and PromptBuilder
    renders (docs/11 §1). Phase 2 only ever populates persona,
    business_rules, user_profile, conversation, and current_utterance —
    the rest default to empty string and are still rendered as empty tags
    (plan decision #1), never omitted, so Phase 4/5 only need to start
    *populating* them.
    """

    model_config = _FROZEN

    persona: str
    business_rules: str
    user_profile: str
    screen_context: str = ""  # Phase 4
    recent_actions: str = ""  # Phase 3/4
    memory_summary: str = ""  # Phase 5
    knowledge: str = ""  # Phase 5
    conversation: tuple[Message, ...] = ()
    current_utterance: str = ""


# --------------------------------------------------------------------------
# Sessions, principals, cost
# --------------------------------------------------------------------------


class SessionUser(BaseModel):
    """The authenticated principal a request/turn runs as. ToolExecutor
    injects this — no tool handler ever accepts a user_id in its own
    argument schema (docs/10 §1 invariant 3)."""

    model_config = _FROZEN

    # min_length=1: a blank principal is a representable-but-invalid state,
    # and this type is the tenant boundary for both tool authorization and
    # memory retrieval. Unreachable today — app/api/deps.py rejects a falsy
    # JWT `sub` before constructing one — but a defence that lives only in
    # the caller is one refactor away from not existing.
    user_id: str = Field(min_length=1)  # merchant_id; the verified JWT `sub` claim
    name: str | None = None


class PendingConfirm(BaseModel):
    """The one held mutating-tool proposal for a session (docs/10 §4).
    Stored in Redis at `session:{id}.pending_confirm`; at most one exists
    at a time — a new proposal supersedes (cancels) the old one."""

    model_config = _FROZEN

    tool: str
    args: dict[str, Any]
    proposed_turn: int
    invocation_id: str


class SafetyVerdict(BaseModel):
    """Result of SafetyLayer.screen_output() — the no-hallucinated-amounts
    and PII-mask check on the assistant's draft reply (docs/05 §3.6)."""

    model_config = _FROZEN

    allowed: bool
    reason: str | None = None
    safe_text: str | None = None  # replacement text when allowed=True but masked/modified


class Session(BaseModel):
    """Mirrors the `conversations` row (docs/12 §4.1)."""

    model_config = _FROZEN

    session_id: str
    user_id: str
    state: SessionState
    started_at: datetime
    ended_at: datetime | None = None


class TurnCost(BaseModel):
    """One turn's usage/cost attribution — CostTracker.record_turn()'s
    return value (docs/05 §3.8)."""

    model_config = _FROZEN

    model: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0
    cost_usd: Decimal
    cost_estimated: bool = False


# --------------------------------------------------------------------------
# LLM provider events (docs/04 §4 — OpenRouterLLM.stream() yields these)
# --------------------------------------------------------------------------


class TokenEvent(BaseModel):
    """One streamed completion-chunk delta. `delta` is the raw
    OpenAI-compatible fragment (`{"content": "..."}` and/or
    `{"tool_calls": [{"index":0,"id":...,"function":{"name":...,"arguments":"..."}}]}`)
    — LLMRouter reassembles fragments across TokenEvents into whole
    ToolCalls (the interface-pin risk called out in the plan)."""

    model_config = _FROZEN

    delta: dict[str, Any]


class UsageEvent(BaseModel):
    """The final usage frame OpenRouter streams when `usage.include: true`
    is set. `usage` is the raw provider dict (prompt_tokens,
    completion_tokens, cached_tokens, ...)."""

    model_config = _FROZEN

    model: str
    usage: dict[str, Any]


class ToolCallsEvent(BaseModel):
    """Whole, reassembled tool calls for one completion — yielded only by
    `LLMRouter.stream()` (app/agent/llm_router.py) after it stitches the
    per-index `tool_calls` fragments carried raw in `TokenEvent.delta`
    back together (docs/05 §3.4's interface pin). `OpenRouterLLM` never
    emits this event.

    Contract note (task 4.3): `LLMRouterProto.stream()`'s frozen docstring
    requires yielding reassembled whole `ToolCall`s, but the original
    `LLMEvent = TokenEvent | UsageEvent` union had no shape to carry them
    — per app/domain/interfaces.py's header rule ("a deviation updates
    the contract file first"), the union gains this member here rather
    than the implementation inventing a private event type.
    """

    model_config = _FROZEN

    tool_calls: tuple[ToolCall, ...]


LLMEvent = TokenEvent | UsageEvent | ToolCallsEvent


# --------------------------------------------------------------------------
# Memory subsystem (Phase 5) — docs/09 owns `user_profiles` and
# `memory_chunks` (canon §11); docs/12 §4.3/§5 owns `conversation_summaries`
# and `kb_articles`. These are the domain twins of the ORM rows in
# app/models/orm.py; a module needing both must alias one of the pair
# (`from app.models.orm import UserProfile as UserProfileRow`), since the
# names are deliberately identical and a plain double import would silently
# shadow the first.
# --------------------------------------------------------------------------

# Frozen by docs/09 §6.1 and docs/16 ADR-003. Both are *contract* constants:
# the DB column is literally `vector(1536)` and the seed/post-call embedders
# must use the same model, or every stored vector becomes incomparable with
# every new query vector (cosine distance between embeddings from different
# models is meaningless, not merely worse). Changing either is a re-embed of
# the whole derived table, never an in-place edit.
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536

# Retrieval defaults, docs/09 §6.2: top-3 by cosine similarity, floor 0.70.
# Below-floor results are dropped, not padded — the model treats retrieved
# text as more authoritative than it is, so an empty RAG slot beats a
# marginal one.
RETRIEVAL_TOP_K = 3
RETRIEVAL_FLOOR = 0.70

# One embedding vector. A tuple (not a list) for the same frozen-value
# discipline the rest of this module applies to `Message.tool_calls` and
# `ContextBundle.conversation`.
#
# Honesty note: Python's type system cannot express "exactly 1536 floats",
# so this alias does NOT carry the dimension. `MemoryChunk` validates the
# length at its own boundary (below) and the `vector(1536)` column rejects
# a wrong-width vector at the DB boundary; a bare `Embedding` passed
# between two functions is checked by neither.
Embedding = tuple[float, ...]


class OpenIssue(BaseModel):
    """One entry in `user_profiles.open_issues` (docs/09 §5.1). This is the
    field that makes the *next* call feel continuous — "checking on your
    limit increase?" is a profile read, not a semantic search.

    `status` is typed `str`, not a Literal: docs/09 §5.1 shows only
    `"pending"` in the canonical Rajesh row and no doc in the set
    enumerates the closed value set, so pinning one here would be
    inventing canon that a later doc would have to contradict. Unlike
    `Resolution` above, there is no DB CHECK to mirror either — this lives
    inside a JSONB column.
    """

    model_config = _FROZEN

    # Length caps throughout: these fields are merchant-influenced through
    # voice -> STT -> Haiku extraction, and they render into the 200-token
    # profile slot (canon §8). Nothing upstream bounds what the extractor
    # can emit, so the cap lives at the validation boundary. Sizes are
    # generous against that slot, not tight — they exist to make a
    # pathological value unrepresentable, not to trim a normal one.
    id: str = Field(max_length=64)  # 'iss_071'
    summary: str = Field(max_length=300)
    status: str = Field(max_length=32)
    opened_call: str = Field(max_length=64)  # session_id that opened it, for provenance
    opened_at: datetime


class UserProfileFacts(BaseModel):
    """The closed extraction schema for `user_profiles.facts`
    (docs/09 §5.2). Every field is optional — a merchant may never state
    their city, and an absent fact must stay absent rather than be guessed.

    **Why a closed schema at all.** docs/09 §5.2: only facts the user
    stated or a tool confirmed may merge; an inference ("sounds
    frustrated", anything demographic) never does. The allowlist is the
    defense against the Haiku extractor inventing fields — the rejected
    alternative was free-form LLM profile append, which reads well in a
    demo and rots in production into a profile slot full of plausible
    fiction.

    **What this model actually enforces, precisely.** Pydantic's default
    `extra="ignore"` means unknown keys are *dropped* at validation, which
    is exactly §5.2's "keys outside the schema are dropped, not stored" —
    deliberately not `extra="forbid"`, which would reject a whole
    otherwise-good extraction because the model volunteered one extra key.
    That is the entire enforcement. It applies only to data that is
    actually passed through this model; nothing in the type system forces
    the post-call merge to validate before writing, and the `facts` column
    is plain JSONB that will accept any shape. The merge path calling
    `UserProfileFacts.model_validate(...)` is what makes the allowlist
    real: that path is `app.memory.user_profile.UserProfileMemory`, which
    validates on both the read and the write side precisely because the
    column does not. It remains true that a *different* writer reaching
    `user_profiles` directly bypasses all of this.

    `merchant_since` is `str`, not `date`: docs/09 §5.1's canonical row
    stores `"2022"` — a stated year, not a calendar date. (The typed
    `DATE` version of this fact is `merchants.merchant_since`, docs/12
    §3.1; this one is what the merchant said out loud.)
    """

    model_config = _FROZEN

    business_name: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, max_length=100)
    merchant_since: str | None = Field(default=None, max_length=32)
    account_type: str | None = Field(default=None, max_length=64)


class UserProfilePreferences(BaseModel):
    """The closed extraction schema for `user_profiles.preferences`
    (docs/09 §5.1/§5.2). Same drop-unknown-keys semantics as
    `UserProfileFacts` — see that docstring for what is and is not
    enforced."""

    model_config = _FROZEN

    language: str | None = Field(default=None, max_length=32)


class UserProfile(BaseModel):
    """Mirrors the `user_profiles` row (docs/09 §5.1 — the owning doc per
    canon §11). Layer 5 of the seven-layer memory model: durable,
    merge-updated post-call, read once at call setup into the 200-token
    profile slot.

    No `merchant_id` foreign key: docs/09 §5.1's DDL declares `user_id
    TEXT PRIMARY KEY` with no `REFERENCES`, and docs/09 wins for this
    table. (docs/12 §1's ER diagram draws `merchants ||--|| user_profiles`
    as if the relation were enforced — it is not, in either doc's DDL.
    Reported as a discrepancy rather than silently reconciled.)
    """

    model_config = _FROZEN

    user_id: str = Field(min_length=1)
    facts: UserProfileFacts = UserProfileFacts()
    preferences: UserProfilePreferences = UserProfilePreferences()
    # Bounded cardinality as well as bounded strings: the merge is
    # append-shaped (docs/09 §5.2) with no doc-specified cap, so an
    # extractor that opens an issue every call would grow this without
    # limit and crowd out the rest of the 200-token profile slot.
    open_issues: tuple[OpenIssue, ...] = Field(default=(), max_length=20)
    updated_at: datetime | None = None  # server-assigned; None before first write
    updated_by_call: str | None = None  # session_id of last merge, for provenance


class ConversationSummary(BaseModel):
    """Mirrors the `conversation_summaries` row (docs/09 §8, docs/12 §4.3).
    The durable end-of-call fold: ≤250 tokens preserving money amounts,
    transaction IDs, tool outcomes, and voiced commitments verbatim
    (docs/09 §4.2).

    `turn_count`/`duration_s`/`cost_usd` duplicate what `conversation_turns`
    and `call_costs` can derive — deliberate, so a summary renders into the
    RAG slot self-contained without a three-way join on the retrieval path
    (docs/12 §4.3).

    Deliberately carries **no embedding**. The vector for this summary
    lives in `memory_chunks` as a `kind='call_summary'` row, written by the
    same post-call pipeline stage. Keeping vectors out of the
    source-of-record tables means re-embedding (model swap, dimension
    change) is a rebuild of one derived table, not an `ALTER` on every
    table that owns text (docs/12 §4.3, §5).
    """

    model_config = _FROZEN

    session_id: str
    user_id: str
    summary: str
    resolution: Resolution
    intents: tuple[str, ...] = ()
    tools_used: tuple[str, ...] = ()
    turn_count: int
    duration_s: int
    cost_usd: Decimal
    created_at: datetime | None = None  # server-assigned; None before insert


class KbArticle(BaseModel):
    """Mirrors the `kb_articles` row (docs/12 §5) — the **source of record**
    for support knowledge, seeded per deploy.

    Deliberately carries **no embedding**. A ~40-article KB chunks into
    ~180 heading-aware ~300-token pieces, and retrieval quality lives at
    chunk granularity — a single article-level vector averages away
    exactly the section the query wanted. Re-chunking or re-embedding is
    `DELETE WHERE kind = 'kb_article'` on the derived table plus a re-run
    of the seed embedder; `kb_articles` itself never changes (docs/12 §5).
    """

    model_config = _FROZEN

    slug: str  # 'kb_daily_limits'
    title: str
    body_md: str
    category: str  # CHECK'd in the DB to the 5 seeded categories
    version: int = 1
    updated_at: datetime | None = None


class MemoryChunk(BaseModel):
    """Mirrors the `memory_chunks` row (docs/09 §6.1 — the owning doc per
    canon §11; reproduced in docs/12 §5). The **derived** embedding store
    for both KB chunks and call summaries, discriminated by `kind`, sharing
    one table and one HNSW cosine index.

    This is the write-side type (seed embedder, post-call embed). The
    read side is `RetrievedMemory`, which carries a similarity score and
    drops the 1536-float vector nothing downstream needs.

    Two invariants are enforced here, both mirroring DB constraints so a
    bad row is rejected at whichever boundary it reaches first:

    1. `len(embedding) == EMBEDDING_DIM`, mirroring the `vector(1536)`
       column width.
    2. `(kind == CALL_SUMMARY) == (user_id is not None)`, mirroring
       `ck_memory_chunks_user_scope`. Both docs' DDL comments say
       "NULL for KB; REQUIRED for call_summary (scoping)" but **neither
       doc's DDL actually declares a constraint for it** — the comment
       claimed more than the SQL delivered. This model and that CHECK are
       what make the word "REQUIRED" true. It matters in both directions:
       a `call_summary` with a NULL `user_id` is unreachable by every
       merchant including its owner (the scoping predicate's `user_id =
       :session_user` is NULL-false), and a `kb_article` carrying a
       `user_id` is the mirror-image bug.
    """

    model_config = _FROZEN

    kind: MemoryKind
    source_id: str = Field(max_length=128)  # kb_articles.slug or conversations.session_id
    # ~300-token KB chunks and ≤250-token summaries (docs/09 §6.1) — this
    # cap is ~6x headroom over both, sized to make a pathological blob
    # unrepresentable rather than to trim legitimate content.
    content: str = Field(max_length=8000)
    embedding: Embedding
    # min_length=1 pairs with `ck_memory_chunks_user_id_not_blank`: without
    # it, `user_id=""` satisfies the biconditional below (it is not None)
    # while matching no real principal — a call summary that looks scoped
    # and is reachable by nobody.
    user_id: str | None = Field(default=None, min_length=1)
    id: int | None = None  # BIGSERIAL; None before insert
    created_at: datetime | None = None  # server-assigned; None before insert

    @field_validator("embedding")
    @classmethod
    def _check_dimension(cls, value: Embedding) -> Embedding:
        if len(value) != EMBEDDING_DIM:
            raise ValueError(
                f"embedding must be {EMBEDDING_DIM}-dimensional "
                f"({EMBEDDING_MODEL}), got {len(value)}"
            )
        return value

    @model_validator(mode="after")
    def _check_user_scope(self) -> MemoryChunk:
        needs_user = self.kind is MemoryKind.CALL_SUMMARY
        has_user = self.user_id is not None
        if needs_user and not has_user:
            raise ValueError(
                "memory_chunks.user_id is required when kind='call_summary' — "
                "an unscoped call summary is unreachable by every merchant, "
                "including its owner (docs/09 §6.1/§6.2)"
            )
        if not needs_user and has_user:
            raise ValueError(
                "memory_chunks.user_id must be NULL when kind='kb_article' — "
                "the KB is globally readable, and a user_id on a KB chunk "
                "misrepresents it as merchant-private (docs/09 §6.1)"
            )
        return self


class RetrievedMemory(BaseModel):
    """One result from `SemanticMemory.retrieve()` — a chunk that cleared
    the similarity floor, plus the score it cleared it by (docs/09 §6.2,
    docs/05 §3.7).

    Contract note, in the style of `ToolCallsEvent` above: docs/05 §3.7
    sketches `retrieve(...) -> list[Chunk]`, but no `Chunk` type is defined
    anywhere in the doc set, and a bare chunk row has nowhere to put the
    cosine score that the top-3/floor-0.70 rule is expressed in. Rather
    than let Batch 2 invent a private shape, the return type is named and
    pinned here.

    The 1536-float vector is deliberately **not** carried: `ContextBuilder`
    renders these into a ~300-token RAG slot and never needs the geometry
    that produced them.

    **`user_id` is carried on purpose, and it is defense in depth.** Every
    other field describes *what* was retrieved; this one describes *whose*
    it is. Without it a leaked foreign call summary is undetectable
    downstream — the caller holds the text but nothing that identifies its
    owner, so no assertion is even expressible. With it, a caller can
    check the scoping outcome independently of whether the SQL that
    produced it was correct:

        assert all(
            r.kind is MemoryKind.KB_ARTICLE or r.user_id == principal.user_id
            for r in results
        )

    That turns "the WHERE clause must be correct" into "the WHERE clause
    must be correct *and* someone must delete an assertion". It is not a
    substitute for the predicate — nothing here runs that check
    automatically, and this class cannot make a caller write it.

    `similarity` is **cosine similarity** (higher is better, 1.0 is
    identical), not the cosine *distance* that pgvector's `<=>` operator
    returns (lower is better). They are related by `similarity = 1 -
    distance`. Getting this backwards would invert the floor comparison
    and admit exactly the marginal results the floor exists to drop, so
    the conversion is the implementation's job and is asserted at the
    bounds below.
    """

    model_config = _FROZEN

    chunk_id: int
    kind: MemoryKind
    source_id: str
    content: str
    similarity: float
    # NULL for a kb_article, the owning merchant for a call_summary — the
    # same biconditional MemoryChunk enforces, re-checked below so a
    # hand-built or faked result cannot describe an ownership state the
    # stored row could never have been in.
    user_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _check_user_scope(self) -> RetrievedMemory:
        if (self.kind is MemoryKind.CALL_SUMMARY) != (self.user_id is not None):
            raise ValueError(
                "RetrievedMemory.user_id must be set for kind='call_summary' and "
                "NULL for kind='kb_article' — mirrors ck_memory_chunks_user_scope, "
                "so an owner-less call summary cannot be represented even in a fake"
            )
        return self

    @field_validator("similarity")
    @classmethod
    def _check_range(cls, value: float) -> float:
        if not -1.0 <= value <= 1.0:
            raise ValueError(
                f"similarity must be a cosine similarity in [-1.0, 1.0], got {value} "
                "(a value outside this range usually means a cosine *distance* "
                "was passed through unconverted)"
            )
        return value
