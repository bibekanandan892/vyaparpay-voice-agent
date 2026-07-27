"""`ShortTermMemory` — Layer 1 of the seven-layer memory model
(docs/09-memory-architecture.md §2): the in-process, per-turn working
scratch space, as distinct from `SessionMemory`'s Redis-backed
persistence (§3, `app/memory/session_memory.py` — see that module's
docstring for the explicit "two things this module is NOT" split).

docs/09 §2 describes its contents as "the assembled `ContextBundle`,
streaming STT partials, in-flight tool calls and their raw results, and
the pending-confirmation record for confirm-gated tools" — "a plain
in-process object, never serialized, discarded when the turn's TTS
dispatch completes" (the one exception, `pending_confirm`, is mirrored
into `SessionMemory` before that discard, because losing it mid-call
would let a confirm-gated mutation execute on state nobody can verify).
`ConversationManager` (docs/05-agent-architecture.md §3.2, Batch 5.1 —
not built yet) is the documented owner: one fresh instance per turn.

Judgment call (Batch 3.4 task brief, honestly flagged rather than
guessed at): docs/17-roadmap.md §1.3's component matrix lists
`ShortTermMemory, SessionMemory` together for Phase 2 with no further
detail, and Phase 2 is text-only (docs/17 §2.2) — there are no streaming
STT partials to hold yet; that slot belongs to Phase 3's voice pipeline
(docs/06-voice-pipeline.md) and is deliberately not built here ahead of a
consumer (YAGNI, ~/.claude/rules/ecc/common/coding-style.md). What
remains from docs/09 §2's description — the in-progress `ContextBundle`,
the turn's accumulated raw `ToolResult`s, and a `pending_confirm` mirror
— is what this class holds, kept intentionally minimal: no method or
field exists here that docs/09 §2 didn't name, since nothing downstream
(`ConversationManager`) is built yet to prove out a richer shape against.

Deliberately mutable, unlike the frozen domain value types in
`app.domain.types`: this is not a value passed between components, it is
the transient accumulator one component writes to in place across a
single turn's tool loop. Nothing else holds a reference to it
concurrently, so immutable replace-on-write would only add ceremony
(`replace(state, tool_results=[*state.tool_results, result])` on every
tool result) for no safety benefit — see the coding-style rule's own
rationale ("prevents hidden side effects... enables safe concurrency"),
neither of which applies to a single-owner, single-turn scratch object.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.domain.types import ContextBundle, PendingConfirm, ToolResult


@dataclass
class ShortTermMemory:
    """One turn's in-process working set. The owner (`ConversationManager`)
    constructs a fresh instance at the start of `on_stt_final` and drops
    it once the turn's response finishes dispatching — after first
    persisting `pending_confirm` into `SessionMemory` if it is set
    (docs/09 §2).
    """

    context_bundle: ContextBundle | None = None
    tool_results: list[ToolResult] = field(default_factory=list)
    pending_confirm: PendingConfirm | None = None

    def record_tool_result(self, result: ToolResult) -> None:
        """Append one raw tool result to this turn's in-flight set
        (docs/09 §2: "in-flight tool calls and their raw results").
        `SessionMemory.format_tool_digest` (a separate, narrower judgment
        call — see that module's docstring) is the compact form a later
        turn actually sees; the raw results held here never leave this
        turn."""
        self.tool_results.append(result)


__all__ = ["ShortTermMemory"]
