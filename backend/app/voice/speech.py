"""SpeechDispatcher — one turn's speaking phase (docs/06 §4.2): reply
text → sentence chunker → per-sentence TTS dispatch → paced egress →
playout. Split out of worker.py for the house size budget, along the seam
judgment call 10 there names: this class is deliberately ReplySink-shaped
(`speak()` = dispatch-sentences + finish-turn), so Batch 6's streaming
brain can feed sentences in without reshaping the worker.

Judgment calls, flagged per house style:

1. **Sentence dispatch is sequential; overlap comes from the egress
   queue.** One `TtsProvider.synthesize()` stream per sentence, awaited
   in order (the provider's per-sentence lifecycle bounds §4.2's
   overlap): sentence n+1 synthesizes while sentence n's audio is still
   *playing* from the paced queue — the §4.2 win — without two provider
   streams interleaving chunks into the per-sentence egress accounting.
2. **A sentence whose TTS stream dies is skipped, and the reply
   continues** — docs/06 §9's mid-stream row verbatim ("skips the
   sentence and continues"). The provider already owns the
   before-first-byte fallback-voice retry; by the time an exception
   reaches this class, that ladder is exhausted. Never silent: every
   skip logs with the sentence number.
3. **SPEAKING is entered on the first genuine audio chunk enqueued**
   (docs/13 §4: "Thinking → Speaking: first TTS chunk dispatched") and
   left only when the queue actually played out — the caller is still
   hearing Asha until `played_ms` reaches the turn's enqueued total.
   The playout wait polls `played_ms` on the injected `sleep` at the
   egress frame cadence; the 2 ms slack absorbs the sub-millisecond
   floor-rounding audio_egress.py documents so the target is always
   reachable.
4. **The agent `transcript.final` is the FULL reply text on the normal
   path** (a barge-in commit sends the truncated one first and sets
   `turn.final_sent`, making this a no-op). A TTS-skipped sentence
   still appears in the final caption: the reply was generated and
   recorded by the brain; the §6.3 played-words truncation contract is
   specifically about *interruption*, not synthesis failure.
5. **Per-sentence audio accounting lives in `SentenceAudio` records on
   the turn** — alignment pairs plus genuine-audio milliseconds — which
   is exactly the input the worker's barge-in commit hands to
   `truncate_reply()` (docs/06 §6.3).
6. **Billable TTS characters are reported at dispatch**, via
   `on_tts_chars`, which the worker forwards to the call's cost ledger
   (docs/16 §5 prices ElevenLabs per character). Counted *before* the
   stream opens, because a sentence whose stream dies part-way
   (judgment call 2) was still submitted and is still billed. What is
   counted is `len(sentence.text)`; the provider's own protocol frames
   and its fallback-voice retry are outside this class's view — see
   `CostTracker.record_tts_text` for exactly which way that skews.

Docs: docs/06 §4.2/§6.3/§9, docs/13 §4. Tests: driven through
VoiceAgentWorker in tests/voice/test_worker.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Final, Protocol, cast

from app.domain.voice import TtsChunk, TtsProvider
from app.obs.logging import get_logger
from app.voice.audio_egress import FRAME_INTERVAL_S, FlushResult
from app.voice.chunker import SentenceChunker
from app.voice.truncation import TTS_PCM_BYTES_PER_MS, SentenceAudio

log = get_logger(__name__)

# Playout-wait under-target guard for resample/floor rounding (≤ ~1 ms per
# chunk boundary, audio_egress judgment call 6) — judgment call 3.
_PLAYOUT_SLACK_MS: Final = 2


class EgressLike(Protocol):
    """What the turn machine needs from `AudioEgress` — narrowed so tests
    assert call ordering on a recording fake (`run()`/`stop()` belong to
    the call-session wiring, not the turn machine)."""

    def enqueue(self, chunk: TtsChunk) -> None: ...

    def drain(self) -> None: ...

    def pause(self) -> None: ...

    def resume(self) -> None: ...

    def flush(self) -> FlushResult: ...

    @property
    def played_ms(self) -> int: ...


@dataclass
class Turn:
    """Mutable per-turn working state shared by the worker's turn task,
    the barge-in commit, and this dispatcher (the audio_egress `_Segment`
    exception to the immutability rule; never returned to callers)."""

    turn_no: int
    baseline_played_ms: int
    sentences: list[SentenceAudio] = field(default_factory=list)
    speaking: bool = False
    interrupted: bool = False
    final_sent: bool = False
    listening_sent: bool = False
    speaking_task: asyncio.Task[None] | None = None

    @property
    def total_audio_ms(self) -> int:
        return sum(s.audio_ms for s in self.sentences)


class SpeechDispatcher:
    """Speaks one reply per `speak()` call. Wire emission stays the
    worker's (the seq counter is per-call, not per-phase): the three
    callbacks are the worker's caption/state senders."""

    def __init__(
        self,
        session_id: str,
        *,
        tts: TtsProvider,
        egress: EgressLike,
        sleep: Callable[[float], Awaitable[None]],
        on_agent_partial: Callable[[int, str], None],
        on_speaking_started: Callable[[int], None],
        on_agent_final: Callable[[int, str], None],
        on_tts_chars: Callable[[int], None],
    ) -> None:
        self._session_id = session_id
        self._tts = tts
        self._egress = egress
        self._sleep = sleep
        self._on_agent_partial = on_agent_partial
        self._on_speaking_started = on_speaking_started
        self._on_agent_final = on_agent_final
        self._on_tts_chars = on_tts_chars

    async def speak(self, turn: Turn, reply_text: str) -> None:
        """Chunk, dispatch, drain, wait for playout, freeze the caption.
        Cancellation (the barge-in commit) can land at any await."""
        chunker = SentenceChunker()
        sentences = [*chunker.feed(reply_text), *chunker.flush()]
        spoken: list[str] = []
        for sentence in sentences:
            spoken.append(sentence.text)
            # Worker judgment call 9: cumulative agent caption at dispatch.
            self._on_agent_partial(turn.turn_no, " ".join(spoken))
            try:
                await self._synthesize_sentence(turn, sentence.text, sentence.sentence_no)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Judgment call 2: §9's skip-and-continue.
                log.warning(
                    "worker.tts_sentence_skipped",
                    session_id=self._session_id,
                    turn=turn.turn_no,
                    sentence_no=sentence.sentence_no,
                    exc_info=True,
                )
        self._egress.drain()
        await self._wait_playout(turn)
        if not turn.final_sent:
            self._on_agent_final(turn.turn_no, reply_text)  # judgment call 4
            turn.final_sent = True

    async def _synthesize_sentence(self, turn: Turn, text: str, sentence_no: int) -> None:
        record = SentenceAudio(sentence_no=sentence_no, text=text)
        turn.sentences.append(record)
        # Judgment call 6: billed at dispatch, before the stream can fail.
        self._on_tts_chars(len(text))
        stream = await self._open_tts_stream(text, sentence_no)
        async with contextlib.aclosing(stream):
            async for chunk in stream:
                if chunk.alignment:
                    record.alignment.extend(chunk.alignment)
                if not chunk.pcm:
                    continue
                self._egress.enqueue(chunk)
                record.audio_ms += len(chunk.pcm) // TTS_PCM_BYTES_PER_MS
                if not turn.speaking:
                    turn.speaking = True  # judgment call 3
                    self._on_speaking_started(turn.turn_no)

    async def _wait_playout(self, turn: Turn) -> None:
        target = turn.baseline_played_ms + max(0, turn.total_audio_ms - _PLAYOUT_SLACK_MS)
        while self._egress.played_ms < target:
            await self._sleep(FRAME_INTERVAL_S)

    async def _open_tts_stream(self, text: str, sentence_no: int) -> AsyncGenerator[TtsChunk, None]:
        """The interfaces.py Protocol wart (`async def ... ->
        AsyncIterator` reads as coroutine-returning while every real
        implementation is an async generator) — the same dual-shape
        handling as ConversationManager._open_stream."""
        raw: object = self._tts.synthesize(text, sentence_no=sentence_no)
        if inspect.iscoroutine(raw):
            raw = await raw
        return cast("AsyncGenerator[TtsChunk, None]", raw)


__all__ = ["EgressLike", "SpeechDispatcher", "Turn"]
