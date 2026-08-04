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

import asyncio
import math
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

from app.domain.types import LLMEvent
from app.domain.voice import SttEvent, TtsChunk


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


class FakeStt:
    """Scriptable `app.domain.voice.SttProvider` double, push-driven: the
    test controls WHEN each event lands (worker turn tests sequence
    partials/finals against VAD frames explicitly), unlike `FakeLLM`'s
    replay-a-script model. One `stream()` call is one 'socket': `push()`
    feeds the currently open stream, `fail()` kills it (the docs/06 §9
    reconnect trigger), and the stream self-ends when the caller's audio
    iterator ends — mirroring DeepgramStt's CloseStream teardown so a
    closed AudioIngress winds a worker down cleanly. `streams_opened`
    counts reconnects; consumed audio accumulates on `.audio`."""

    def __init__(self) -> None:
        self.streams_opened = 0
        self.audio: list[bytes] = []
        self._active: asyncio.Queue[SttEvent | BaseException | None] | None = None

    def push(self, event: SttEvent) -> None:
        """Emit one event from the currently open stream."""
        if self._active is None:
            raise AssertionError(
                "FakeStt.push() with no open stream — wait for streams_opened "
                "to advance before pushing events."
            )
        self._active.put_nowait(event)

    def fail(self, exc: BaseException) -> None:
        """Kill the currently open stream with `exc` (a mid-stream death)."""
        if self._active is None:
            raise AssertionError("FakeStt.fail() with no open stream")
        self._active.put_nowait(exc)

    async def stream(
        self, audio: AsyncIterator[bytes], *, sample_rate: int
    ) -> AsyncIterator[SttEvent]:
        self.streams_opened += 1
        queue: asyncio.Queue[SttEvent | BaseException | None] = asyncio.Queue()
        self._active = queue

        async def drain() -> None:
            try:
                async for chunk in audio:
                    self.audio.append(chunk)
            finally:
                # Audio over -> stream over (the CloseStream analogue).
                queue.put_nowait(None)

        drain_task = asyncio.create_task(drain())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    return
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            drain_task.cancel()
            await asyncio.gather(drain_task, return_exceptions=True)


# TtsChunk.pcm is 24 kHz mono s16 (app/domain/voice.py) -> 24 samples/ms.
_TTS_SAMPLES_PER_MS = 24
# FakeTts default synthesis pace: 10 ms of audio per character.
FAKE_TTS_MS_PER_CHAR = 10


def _square_wave_pcm(duration_ms: int, *, amplitude: int = 12_000) -> bytes:
    """Loud deterministic s16le audio (600 Hz square) — survives Opus in
    the fake-peer E2E, unlike silence."""
    total = duration_ms * _TTS_SAMPLES_PER_MS
    out = bytearray()
    for i in range(total):
        value = amplitude if math.sin(2 * math.pi * 600 * i / 24_000) >= 0 else -amplitude
        out += value.to_bytes(2, "little", signed=True)
    return bytes(out)


def fake_tts_chunk(text: str, *, sentence_no: int = 0) -> TtsChunk:
    """The default FakeTts synthesis for one sentence: 10 ms per char,
    per-char alignment `(i, 10*i)` — deterministic input for the docs/06
    §6.3 truncation math."""
    duration_ms = FAKE_TTS_MS_PER_CHAR * len(text)
    return TtsChunk(
        pcm=_square_wave_pcm(duration_ms),
        alignment=tuple((i, FAKE_TTS_MS_PER_CHAR * i) for i in range(len(text))),
        sentence_no=sentence_no,
    )


@dataclass
class _TtsScript:
    chunks: tuple[TtsChunk, ...]
    fail_after: int | None  # raise after yielding this many chunks
    error: BaseException | None
    hold: asyncio.Event | None  # await before ending the stream


class FakeTts:
    """Scriptable `app.domain.voice.TtsProvider` double. Unscripted text
    synthesizes one deterministic `fake_tts_chunk`; `script()` pins exact
    chunks per sentence text, an optional mid-stream failure point
    (docs/06 §9's skip-and-continue trigger), and an optional `hold`
    event that keeps the stream in-flight so barge-in tests can cancel a
    genuinely live synthesis. Calls are recorded as (text, sentence_no)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self._scripts: dict[str, _TtsScript] = {}

    def script(
        self,
        text: str,
        *chunks: TtsChunk,
        fail_after: int | None = None,
        error: BaseException | None = None,
        hold: asyncio.Event | None = None,
    ) -> None:
        self._scripts[text] = _TtsScript(
            chunks=chunks, fail_after=fail_after, error=error, hold=hold
        )

    async def synthesize(self, text: str, *, sentence_no: int) -> AsyncIterator[TtsChunk]:
        self.calls.append((text, sentence_no))
        script = self._scripts.get(text)
        if script is None:
            yield fake_tts_chunk(text, sentence_no=sentence_no)
            return
        for i, chunk in enumerate(script.chunks):
            if script.fail_after is not None and i == script.fail_after:
                raise script.error or RuntimeError(f"FakeTts scripted failure on {text!r}")
            yield chunk.model_copy(update={"sentence_no": sentence_no})
        if script.fail_after is not None and script.fail_after >= len(script.chunks):
            raise script.error or RuntimeError(f"FakeTts scripted failure on {text!r}")
        if script.hold is not None:
            await script.hold.wait()


class FakeBrain:
    """Scriptable `app.voice.worker.Brain` double — one reply per
    expected `on_stt_final()` call, FIFO, same run-dry discipline as
    `FakeLLM`/`FakeVad`. Utterances received are recorded on `.calls`."""

    def __init__(self) -> None:
        self._replies: list[str] = []
        self.calls: list[str] = []
        self._hold: asyncio.Event | None = None

    def script(self, *replies: str) -> None:
        self._replies.extend(replies)

    def hold_next_reply(self, event: asyncio.Event) -> None:
        """Block the NEXT `on_stt_final()` call until `event` is set, then
        clear the hold — lets a test keep a turn parked in THINKING (the
        brain call in flight) while it drives VAD/STT events that must
        land before that call resolves (worker judgment call 4: an
        endpoint arriving mid-turn is queued, not opened as a new turn)."""
        self._hold = event

    async def on_stt_final(self, text: str) -> str:
        self.calls.append(text)
        if not self._replies:
            raise AssertionError(
                "FakeBrain.on_stt_final() called with no scripted reply left — "
                "script one reply per expected turn."
            )
        if self._hold is not None:
            hold, self._hold = self._hold, None
            await hold.wait()
        return self._replies.pop(0)


__all__ = [
    "FAKE_TTS_MS_PER_CHAR",
    "FakeBrain",
    "FakeLLM",
    "FakeLLMCall",
    "FakeStt",
    "FakeTts",
    "FakeVad",
    "fake_tts_chunk",
]
