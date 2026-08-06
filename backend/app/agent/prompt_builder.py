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

---
## Slot-boundary escaping (Phase-5 memory wiring)

docs/08 §5.3 puts the data-fencing of untrusted slots in this module:
"`ContextBuilder` decides *what* the model sees, `PromptBuilder` decides
*how* it is framed." The tagged regions below are the framing, so a slot
whose *content* contains a slot tag is this module's problem.

Until Phase 5 nothing merchant-influenced reached a slot that could
exploit that: slots 4/5 carry device-captured screen text (length-capped
and control-character-stripped on-device, docs/07 §5 rule 6), and slots
8/9 are separate `Message` objects that never enter the system string at
all. Phase 5 changes it — the profile slot now carries `open_issues`
text and the knowledge slot carries retrieved chunks, both durable and
both derived from what a merchant said. A stored `</user_profile>` fits
inside `OpenIssue.summary`'s 300-char cap and survives
`UserProfileMemory._flatten` unchanged (that module's own tests pin it),
so without this the row could close its own region and everything after
it would read as peer-level framing rather than profile content.

`escape_slot_tags` therefore neutralizes slot-tag-shaped tokens in slot
content — **every slot, not only the memory ones**. Defending only the
slots believed untrusted today is how the next slot to become untrusted
gets missed, and the cost of uniformity is one regex pass over strings
the module already concatenates. The escape is `<` -> `&lt;`, `>` ->
`&gt;`, applied only to the matched token, so `<voice_rules>` and the
other persona sub-blocks (docs/11 §2/§5) are untouched — they are not
slot tags.

### Exactly what it neutralizes, and what it does not

It rewrites a **well-formed** tag token: an optional slash, the tag name,
anything that is not `>`, then a closing `>`. Case-insensitive, tolerant
of whitespace and of an attribute-looking tail the renderer never emits.

It does **not** rewrite an unterminated `</user_profile` with no closing
`>` (security review L1). That is reachable — `app/memory/slots.py`
truncates entries at a character budget, so a cut can land exactly on the
`>` — and it is left alone deliberately rather than by oversight:
widening the pattern to `(?:>|$)` would let a single stray `<` consume
every character to the end of the slot and escape a span that was never
tag-shaped, which is a worse failure than the residue. What survives is
malformed residue, not a boundary: the renderer's own `</user_profile>`
still closes the region in the right place, and the review found no
input across ~30 variants that produced a well-formed forged boundary
this way. `tests/agent/test_prompt_builder.py` pins the residue so it
stays a recorded choice.

It also does not make injected prose stop being persuasive. Text inside
a correctly-closed region can still read like an instruction, and the
answer to that is the `<fencing_rules>` block in `prompts/persona.md`
(which Phase 5 extended to name all five untrusted slots) plus the
action-side controls — confirm gate, principal injection, tool allowlist
— not this function. `app.agent.safety_layer.looks_injected` catches a
short list of imperative forms and is explicitly not a general filter:
it does not match "this caller is pre-authorised", "no confirmation
required", or similar plausible-sounding policy prose.

### Budgets are measured against the escaped form

`escape_slot_tags` is public because escaping *expands* content (up to
~1.62x on a string that is all tag tokens), so a slot rendered at its
budget can exceed it once escaped (security review M6). The slot
renderers in `app/memory/slots.py` therefore measure this function's
output when enforcing their budgets, while this module remains the only
place that actually applies it — one definition, measured and applied
through the same call, so the two cannot disagree.
"""

from __future__ import annotations

import re
from typing import Final

from app.domain.types import ContextBundle, Message, Role

# docs/11 §1's slot tags, in the template's own order. The literal tuple
# is the single definition: `render` iterates it, and `_SLOT_TAG_TOKEN`
# is built from it, so a slot added to one is added to both.
SLOT_TAGS: Final = (
    "persona",
    "business_rules",
    "user_profile",
    "screen_context",
    "recent_actions",
    "memory_summary",
    "knowledge",
)

# A WELL-FORMED tag token — the `>` is required, and the module
# docstring's L1 section says why the alternative is worse. Either
# polarity (`<tag>` / `</tag>`), any case, tolerating whitespace around
# the slash and any attribute-looking tail the renderer itself never
# emits. `\b` after the name keeps `<knowledge_base>` from matching —
# `_` is a word character, so the boundary only falls where the name
# genuinely ends.
_SLOT_TAG_TOKEN: Final = re.compile(
    r"<\s*/?\s*(?:" + "|".join(SLOT_TAGS) + r")\b[^>]*>", re.IGNORECASE
)


def escape_slot_tags(content: str) -> str:
    """Neutralize well-formed slot-tag tokens inside slot content.

    Escaping (rather than deleting) keeps the text legible to the model
    and to whoever reads the trace: a merchant whose business name really
    is odd still has it rendered, just unable to act as a delimiter.

    Public so `app/memory/slots.py` can measure its output against the
    slot budgets — see the module docstring's last two sections for what
    this does and does not neutralize, and why the budgets have to be
    taken after it.
    """
    return _SLOT_TAG_TOKEN.sub(
        lambda match: match.group(0).replace("<", "&lt;").replace(">", "&gt;"), content
    )

# Stable and deterministic — same bytes on every call-open turn.
CALL_OPEN_TRIGGER = (
    "[SYSTEM TRIGGER: call connected. Greet the merchant and ask how you "
    "can help. Do not invent account facts.]"
)


def _render_slot(tag: str, content: str) -> str:
    """One tagged slot region. Empty content collapses to a literal empty
    tag pair on one line — exactly how docs/11 §4 shows
    `<memory_summary></memory_summary>` on the worked turn-1 example.

    The escape runs here, at the one place the tags are written, so no
    caller can supply pre-fenced content and no future slot can be added
    without it (see the module docstring)."""
    if not content:
        return f"<{tag}></{tag}>"
    return f"<{tag}>\n{escape_slot_tags(content)}\n</{tag}>"


class PromptBuilder:
    """Implements `PromptBuilderProto` (app/domain/interfaces.py)."""

    def render(self, bundle: ContextBundle) -> list[Message]:
        # Literal tuple, not a dict: the fixed order IS the contract
        # (docs/11 §1) and iteration over a literal can't reorder the way
        # a dict-ordering leak could (docs/08 §5.1's warned failure mode).
        # Spelled out here rather than `getattr(bundle, tag) for tag in
        # SLOT_TAGS` so mypy still checks every slot name against
        # `ContextBundle`; the two lists agreeing is pinned by
        # tests/agent/test_prompt_builder.py instead, which is what makes
        # a slot added here without an escape entry fail the build.
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


__all__ = ["CALL_OPEN_TRIGGER", "SLOT_TAGS", "PromptBuilder", "escape_slot_tags"]
