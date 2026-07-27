"""Unit tests for app.agent.prompt_builder.PromptBuilder.

Pure-function tests — `render()` takes a `ContextBundle` and returns
`list[Message]`, no I/O to fake. The bundles here use short sentinel
strings rather than the real prompt files: the template's *shape*
(docs/11 §1's tag order, empty-tag rendering, the §1.1 prefix
invariant, the §4 turn-1 trigger) is what this file pins; the real
prompt-file bytes are pinned in test_context_builder.py.
"""

from __future__ import annotations

from app.agent.prompt_builder import CALL_OPEN_TRIGGER, PromptBuilder
from app.domain.interfaces import PromptBuilderProto
from app.domain.types import ContextBundle, Message, Role

_SLOT_TAGS_IN_ORDER = (
    "persona",
    "business_rules",
    "user_profile",
    "screen_context",
    "recent_actions",
    "memory_summary",
    "knowledge",
)


def make_bundle(**overrides: object) -> ContextBundle:
    defaults: dict[str, object] = {
        "persona": "PERSONA-TEXT",
        "business_rules": "RULES-TEXT",
        "user_profile": "PROFILE-TEXT",
        "conversation": (
            Message(role=Role.USER, content="my payment failed"),
            Message(role=Role.ASSISTANT, content="Let me check that."),
        ),
        "current_utterance": "what can I do?",
    }
    defaults.update(overrides)
    return ContextBundle(**defaults)  # type: ignore[arg-type]


def test_prompt_builder_satisfies_the_frozen_protocol() -> None:
    builder: PromptBuilderProto = PromptBuilder()
    assert isinstance(builder, PromptBuilder)


# --------------------------------------------------------------------------
# Message-list structure: 1 system + window + final user (docs/11 §1)
# --------------------------------------------------------------------------


def test_render_produces_system_then_window_then_final_user() -> None:
    bundle = make_bundle()

    messages = PromptBuilder().render(bundle)

    assert [m.role for m in messages] == [Role.SYSTEM, Role.USER, Role.ASSISTANT, Role.USER]
    assert messages[-1].content == "what can I do?"


def test_render_has_exactly_one_system_message_and_it_is_first() -> None:
    messages = PromptBuilder().render(make_bundle())

    assert messages[0].role is Role.SYSTEM
    assert sum(1 for m in messages if m.role is Role.SYSTEM) == 1


def test_window_messages_pass_through_verbatim_as_real_chat_turns() -> None:
    """docs/11 §1.1: slots 8-9 are native chat turns, not tagged text in
    the system message."""
    window = (
        Message(role=Role.USER, content="turn one"),
        Message(role=Role.ASSISTANT, content="turn two"),
    )
    bundle = make_bundle(conversation=window)

    messages = PromptBuilder().render(bundle)

    assert messages[1:3] == list(window)
    assert "turn one" not in (messages[0].content or "")


# --------------------------------------------------------------------------
# System-message template: tag order and empty-tag rendering (docs/11 §1)
# --------------------------------------------------------------------------


def test_system_message_renders_all_seven_tags_in_the_exact_s1_order() -> None:
    content = PromptBuilder().render(make_bundle())[0].content
    assert content is not None

    positions = [content.index(f"<{tag}>") for tag in _SLOT_TAGS_IN_ORDER]

    assert positions == sorted(positions)
    for tag in _SLOT_TAGS_IN_ORDER:
        assert content.count(f"<{tag}>") == 1
        assert content.count(f"</{tag}>") == 1


def test_phase2_empty_slots_render_as_literal_empty_tag_pairs() -> None:
    """Plan decision #1: slots 4-7 are rendered, never omitted, so Phase
    4/5 only start populating them."""
    content = PromptBuilder().render(make_bundle())[0].content
    assert content is not None

    assert "<screen_context></screen_context>" in content
    assert "<recent_actions></recent_actions>" in content
    assert "<memory_summary></memory_summary>" in content
    assert "<knowledge></knowledge>" in content


def test_populated_slots_wrap_their_content_in_tags() -> None:
    content = PromptBuilder().render(make_bundle())[0].content
    assert content is not None

    assert "<persona>\nPERSONA-TEXT\n</persona>" in content
    assert "<business_rules>\nRULES-TEXT\n</business_rules>" in content
    assert "<user_profile>\nPROFILE-TEXT\n</user_profile>" in content


# --------------------------------------------------------------------------
# Prefix-cache invariant (docs/11 §1.1)
# --------------------------------------------------------------------------


def _prefix_through_user_profile(content: str) -> str:
    marker = "</user_profile>"
    return content[: content.index(marker) + len(marker)]


def test_slot_1_to_3_region_is_byte_identical_across_volatile_changes() -> None:
    """Two turns of the same call: only conversation/current_utterance
    differ -> the system text up through </user_profile> (the cache
    breakpoint, docs/11 §1.1) must be byte-identical."""
    turn_2 = make_bundle(
        conversation=(Message(role=Role.USER, content="my payment failed"),),
        current_utterance="why did it fail?",
    )
    turn_3 = make_bundle(
        conversation=(
            Message(role=Role.USER, content="my payment failed"),
            Message(role=Role.ASSISTANT, content="Your daily limit was exceeded."),
            Message(role=Role.USER, content="why did it fail?"),
        ),
        current_utterance="raise my limit please",
    )

    content_2 = PromptBuilder().render(turn_2)[0].content
    content_3 = PromptBuilder().render(turn_3)[0].content
    assert content_2 is not None and content_3 is not None

    assert _prefix_through_user_profile(content_2) == _prefix_through_user_profile(content_3)
    # In Phase 2 slots 4-7 are empty on every turn, so the ENTIRE system
    # message is stable across the call, not just the breakpoint region.
    assert content_2 == content_3


def test_render_is_deterministic_for_the_same_bundle() -> None:
    """Same bundle in, byte-identical prompt out (docs/08 §5.1's tested
    property — no timestamps or ordering leakage anywhere)."""
    bundle = make_bundle()

    first = PromptBuilder().render(bundle)
    second = PromptBuilder().render(bundle)

    assert first == second


# --------------------------------------------------------------------------
# Turn-1 synthetic call-open trigger (docs/11 §4; Phase-2 adaptation)
# --------------------------------------------------------------------------


def test_turn_1_renders_the_synthetic_call_open_trigger() -> None:
    """Convention: empty window AND empty utterance is call-open — the
    agent speaks first (docs/11 §4)."""
    bundle = make_bundle(conversation=(), current_utterance="")

    messages = PromptBuilder().render(bundle)

    assert [m.role for m in messages] == [Role.SYSTEM, Role.USER]
    assert messages[-1].content == CALL_OPEN_TRIGGER


def test_call_open_trigger_is_stable_and_screen_free() -> None:
    """Phase 2 has no screen (plan decision #2): the trigger asks for a
    profile-only greeting and must not reference on-screen content the
    way docs/11 §4's Phase-4 version does."""
    assert CALL_OPEN_TRIGGER.startswith("[SYSTEM TRIGGER: call connected.")
    assert "Do not invent account facts." in CALL_OPEN_TRIGGER
    assert "screen" not in CALL_OPEN_TRIGGER.lower()


def test_first_spoken_utterance_on_an_empty_window_is_not_a_trigger() -> None:
    """A non-empty utterance means the user spoke — never synthesize the
    trigger over real speech, even with no history yet."""
    bundle = make_bundle(conversation=(), current_utterance="hello?")

    messages = PromptBuilder().render(bundle)

    assert messages[-1].content == "hello?"


def test_empty_utterance_mid_call_passes_through_not_a_trigger() -> None:
    """Defensive half of the convention: a non-empty window can never be
    call-open — an empty utterance there is passed through verbatim
    (deterministic) rather than re-triggering the greeting."""
    bundle = make_bundle(
        conversation=(Message(role=Role.USER, content="hi"),),
        current_utterance="",
    )

    messages = PromptBuilder().render(bundle)

    assert messages[-1].role is Role.USER
    assert messages[-1].content == ""
