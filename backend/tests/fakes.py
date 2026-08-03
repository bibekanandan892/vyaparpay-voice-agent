"""Shared test doubles every later batch's test module imports instead of
hand-rolling its own — see tests/conftest.py's `fake_llm` fixture for the
usual entry point.

`FakeLLM` implements the same `app.domain.interfaces.LLMProvider` Protocol
`app.providers.openrouter.OpenRouterLLM` does, but replays a pre-scripted
sequence of events instead of making an HTTP call — for pure in-process
unit tests of components downstream of the provider layer (LLMRouter,
ConversationManager, ...) that don't need or want HTTP-level mocking via
respx. Explicitly called out in the Phase-2 plan as needed by task 6.2's
end-to-end canonical-conversation test, which scripts a distinct LLM
response per conversation turn — so this is deliberately generic (an
ordered queue of turns, each an ordered sequence of events) rather than
tailored to any one test's shape.

Docs: docs/04-backend-architecture.md §4 (`LLMProvider` protocol), §9.4
(provider mocking strategy — respx for wire-level tests, a fake for
pure in-process ones).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

from app.domain.types import LLMEvent


@dataclass(frozen=True)
class FakeLLMCall:
    """One recorded `FakeLLM.stream()` invocation — inspect `fake_llm.calls`
    in a test to assert what a caller actually sent (e.g. "the reassembled
    tool call's args were exactly ...", "the tools array was passed
    through unmodified")."""

    messages: list[dict[str, Any]]
    models: list[str]
    tools: list[dict[str, Any]] | None


class FakeLLM:
    """Scriptable `LLMProvider` double.

    Queue one turn's worth of events per expected `stream()` call, in call
    order, via `.script_turn(...)` (or `.script(...)` for several turns at
    once). Each `stream()` call pops the next queued turn off the front of
    the queue and replays its events; calling `stream()` with nothing left
    queued is a test-authoring bug, so it raises loudly rather than
    silently yielding nothing.
    """

    def __init__(self) -> None:
        self._turns: list[list[LLMEvent]] = []
        self.calls: list[FakeLLMCall] = []

    def script_turn(self, *events: LLMEvent) -> None:
        """Queue a single turn's worth of events, replayed in order the
        next time `.stream()` is called."""
        self._turns.append(list(events))

    def script(self, *turns: Sequence[LLMEvent]) -> None:
        """Queue several turns at once, in the order they'll be consumed —
        `fake_llm.script(turn_1_events, turn_2_events, ...)` is equivalent
        to calling `.script_turn(*events)` once per turn."""
        for turn in turns:
            self._turns.append(list(turn))

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        models: list[str],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        self.calls.append(FakeLLMCall(messages=messages, models=models, tools=tools))
        if not self._turns:
            raise AssertionError(
                "FakeLLM.stream() called with no scripted turn queued — call "
                ".script_turn(...) once per expected stream() call before "
                "exercising the code under test."
            )
        turn = self._turns.pop(0)
        for event in turn:
            yield event


class FakeVad:
    """Scriptable `app.domain.voice.VadModel` double — one scripted speech
    probability per expected `prob()` call, in call order, so endpoint-
    policy tests (tests/voice/test_vad_endpointer.py) drive the §5.3
    decision table deterministically with no onnxruntime in the loop —
    exactly the split `VadModel`'s docstring promises.

    Same discipline as `FakeLLM`: script via `.script(...)` (or the
    constructor), one prob is consumed per `prob()` call, and running dry
    is a test-authoring bug that raises loudly rather than silently
    returning a default that would masquerade as silence. Frames passed
    in are recorded on `.calls` for the rare test that asserts what audio
    actually reached the model.
    """

    def __init__(self, probs: Sequence[float] = ()) -> None:
        self._probs: list[float] = list(probs)
        self.calls: list[bytes] = []

    def script(self, *probs: float) -> None:
        """Append probabilities to the script, consumed FIFO — one per
        upcoming `prob()` call."""
        self._probs.extend(probs)

    def prob(self, frame_30ms: bytes) -> float:
        self.calls.append(frame_30ms)
        if not self._probs:
            raise AssertionError(
                "FakeVad.prob() called with no scripted probability left — call "
                ".script(...) with one probability per frame the code under test "
                "will process."
            )
        return self._probs.pop(0)


__all__ = ["FakeLLM", "FakeLLMCall", "FakeVad"]
