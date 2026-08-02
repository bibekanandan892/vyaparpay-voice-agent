"""Unit tests for the pure/testable units of `scripts/demo_cli.py` (task
6.1). `_run` itself needs a live Postgres + Redis + OpenRouter key and is
therefore not exercised here (matching `test_seed.py`'s own no-Docker
approach) — this file covers `_parse_args` and `_repl`, which need
neither: `_repl` only depends on an object exposing an async
`on_stt_final(str) -> str`, driven here by a scripted fake rather than a
real `ConversationManager`.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import patch

from scripts.demo_cli import _parse_args, _repl


class _FakeManager:
    """Records every utterance passed to `on_stt_final` and returns a
    scripted reply, standing in for `ConversationManager` — `_repl`
    never touches anything else on the real class."""

    def __init__(self) -> None:
        self.utterances: list[str] = []

    async def on_stt_final(self, text: str) -> str:
        self.utterances.append(text)
        return f"reply to: {text}"


def _inputs(*lines: str) -> Iterator[str]:
    yield from lines


# --------------------------------------------------------------------------
# _parse_args
# --------------------------------------------------------------------------


def test_parse_args_defaults_to_the_seeded_demo_merchant() -> None:
    args = _parse_args([])
    assert args.user == "usr_rajesh01"


def test_parse_args_accepts_a_user_override() -> None:
    args = _parse_args(["--user", "usr_other02"])
    assert args.user == "usr_other02"


# --------------------------------------------------------------------------
# _repl
# --------------------------------------------------------------------------


async def test_repl_forwards_each_line_and_counts_turns() -> None:
    manager = _FakeManager()
    with patch("builtins.input", side_effect=_inputs("hello", "how are you", "/end")):
        turns = await _repl(manager)  # type: ignore[arg-type]

    assert turns == 2
    assert manager.utterances == ["hello", "how are you"]


async def test_repl_stops_on_slash_end_without_forwarding_it() -> None:
    manager = _FakeManager()
    with patch("builtins.input", side_effect=_inputs("/end")):
        turns = await _repl(manager)  # type: ignore[arg-type]

    assert turns == 0
    assert manager.utterances == []


async def test_repl_stops_on_eof_without_crashing() -> None:
    manager = _FakeManager()
    with patch("builtins.input", side_effect=EOFError):
        turns = await _repl(manager)  # type: ignore[arg-type]

    assert turns == 0
    assert manager.utterances == []


async def test_repl_stops_on_keyboard_interrupt_without_crashing() -> None:
    manager = _FakeManager()
    with patch("builtins.input", side_effect=KeyboardInterrupt):
        turns = await _repl(manager)  # type: ignore[arg-type]

    assert turns == 0
    assert manager.utterances == []


async def test_repl_reprompts_on_a_blank_line_instead_of_forwarding_it() -> None:
    """Review MEDIUM, fixed: a blank Enter-press must not reach
    on_stt_final — PromptBuilder's turn-1 convention only applies to the
    FIRST (already-handled) empty utterance, not a mid-call one."""
    manager = _FakeManager()
    with patch("builtins.input", side_effect=_inputs("", "  ", "real question", "/end")):
        turns = await _repl(manager)  # type: ignore[arg-type]

    assert turns == 1
    assert manager.utterances == ["real question"]


async def test_repl_strips_whitespace_before_forwarding() -> None:
    manager = _FakeManager()
    with patch("builtins.input", side_effect=_inputs("  hi there  ", "/end")):
        await _repl(manager)  # type: ignore[arg-type]

    assert manager.utterances == ["hi there"]
