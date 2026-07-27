"""`PromptBuilder` — renders a `ContextBundle` into the message list
(docs/05-agent-architecture.md §3.3; the template is owned entirely by
docs/11-prompt-engineering.md §1).

Output shape: ONE system `Message` carrying slots 1–7 as XML-ish tagged
regions in docs/11 §1's exact order, then the conversation window as
real chat turns (slot 8), then one final `user` message with the current
utterance (slot 9). The system/message-array split is deliberate
(docs/11 §1.1): the window and utterance are dialogue the model treats
as turns; the tagged slots are reference framing — which is also what
makes the §3 injection fence defensible.

Prefix-cache invariant (docs/11 §1.1): the system text is built by pure
string concatenation over the bundle's slot strings, in a fixed literal
order — no timestamps, no dict iteration, no per-turn content above the
breakpoint — so the slot 1–3 region is byte-identical across turns of a
call whenever the bundle's persona/business_rules/user_profile are.
Empty slots render as literal empty tag pairs (plan decision #1), e.g.
`<screen_context></screen_context>` — never omitted — so Phase 4/5 only
start populating them, never restructuring the template.

Turn-1 convention (documented decision, docs/11 §4): the caller signals
call-open by rendering a bundle with an EMPTY conversation window AND an
EMPTY current_utterance — the state that only exists before anyone has
spoken. The final user message is then the synthetic `CALL_OPEN_TRIGGER`
below. Any non-empty utterance, or any prior turn in the window, renders
the utterance verbatim instead (an empty utterance mid-call passes
through as an empty user message — the caller's bug, kept deterministic
rather than guessed at). The trigger is Phase 2's no-screen adaptation
of docs/11 §4's version (plan decision #2: profile-only greeting — there
is no screen issue to address yet); "greet by name" was dropped because
the Phase-2 profile slot carries no personal name (see
context_builder.py judgment call #2).

Accepted residual risk (review MEDIUM, deliberately not "fixed"): if a
Redis failure degrades the window to empty (context_builder judgment
call #4) on the same mid-call turn whose utterance is ALSO empty, the
two emptinesses are indistinguishable from call-open and the trigger
renders again. Threading an explicit turn number through would mean
changing the frozen `PromptBuilderProto`/`ContextBundle` contracts for a
double-degenerate edge whose worst case is a redundant greeting — the
regression test pins the behavior so it is a documented choice, not an
accident.
"""

from __future__ import annotations

from app.domain.types import ContextBundle, Message, Role

# Stable and deterministic — same bytes on every call-open turn.
CALL_OPEN_TRIGGER = (
    "[SYSTEM TRIGGER: call connected. Greet the merchant and ask how you "
    "can help. Do not invent account facts.]"
)


def _render_slot(tag: str, content: str) -> str:
    """One tagged slot region. Empty content collapses to a literal empty
    tag pair on one line — exactly how docs/11 §4 shows
    `<memory_summary></memory_summary>` on the worked turn-1 example."""
    if not content:
        return f"<{tag}></{tag}>"
    return f"<{tag}>\n{content}\n</{tag}>"


class PromptBuilder:
    """Implements `PromptBuilderProto` (app/domain/interfaces.py)."""

    def render(self, bundle: ContextBundle) -> list[Message]:
        # Literal tuple, not a dict: the fixed order IS the contract
        # (docs/11 §1) and iteration over a literal can't reorder the way
        # a dict-ordering leak could (docs/08 §5.1's warned failure mode).
        system_text = "\n\n".join(
            _render_slot(tag, content)
            for tag, content in (
                ("persona", bundle.persona),
                ("business_rules", bundle.business_rules),
                ("user_profile", bundle.user_profile),
                ("screen_context", bundle.screen_context),
                ("recent_actions", bundle.recent_actions),
                ("memory_summary", bundle.memory_summary),
                ("knowledge", bundle.knowledge),
            )
        )

        is_call_open = not bundle.conversation and not bundle.current_utterance
        final_text = CALL_OPEN_TRIGGER if is_call_open else bundle.current_utterance

        # Defense-in-depth (review MEDIUM): the one-system-message design
        # is what the docs/11 §3 injection fence leans on — if a SYSTEM-
        # role entry ever leaked into the Redis window (a bug elsewhere),
        # splicing it here would mint a second pseudo-authoritative
        # region. Filtered, logged loudly upstream by whoever wrote it;
        # dropping it beats voicing it.
        window = tuple(m for m in bundle.conversation if m.role is not Role.SYSTEM)

        return [
            Message(role=Role.SYSTEM, content=system_text),
            *window,
            Message(role=Role.USER, content=final_text),
        ]


__all__ = ["CALL_OPEN_TRIGGER", "PromptBuilder"]
