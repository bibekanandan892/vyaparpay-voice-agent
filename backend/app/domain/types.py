"""Shared domain value types for the agent loop.

These are the interchange types every module in app/agent/, app/tools/,
app/memory/, and app/providers/ passes between each other — pinned once
here (per the Phase-2 plan's "contract freeze" batch) so parallel work on
those packages doesn't each invent a slightly different ToolResult or
Message shape.

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

from pydantic import BaseModel, ConfigDict

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

    user_id: str  # merchant_id; the verified JWT `sub` claim
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


LLMEvent = TokenEvent | UsageEvent
