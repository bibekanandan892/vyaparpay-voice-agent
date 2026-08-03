"""SttSupervisor — the worker's STT-stream lifecycle owner: one pump over
the single-subscriber ingress fan-out, one provider stream at a time, and
the docs/06 §9 reconnect policy ("Worker reconnects the Deepgram stream
immediately"; the honest re-ask is the TURN's job, triggered through
`on_stream_died`). Split out of worker.py to keep that file inside the
house size budget — this is the worker's own seam, not a new contract.

Judgment calls, flagged per house style:

1. **One pump task drains `ingress.stt_stream()` into a supervisor-owned
   queue for the whole call; each provider `stream()` attempt gets a
   FRESH generator over that queue.** DeepgramStt's sender cancels a
   pending `anext()` on teardown, which kills the async generator it was
   iterating — feeding the ingress iterator directly to the provider
   would burn the single-subscriber fan-out on the first reconnect
   (AudioIngress judgment call 7 makes re-subscription a hard error).
   Audio queued while reconnecting is consumed by the next attempt —
   docs/06 §9's "AudioIngress buffers briefly during the reconnect".
2. **Reconnect budget: one immediate reconnect per death; two
   consecutive deaths with no event in between are terminal.** A stream
   that yielded events before dying earns a fresh budget (a mid-call
   blip); one that dies instantly twice means the provider/key/network
   is genuinely down — log an error and stop rather than loop hot. The
   call itself continues (degraded per §9's ladder); teardown is the
   call session's job.
3. **`on_stream_died` fires before the reconnect attempt** so a turn
   waiting on its final learns immediately (and can speak the §9
   re-ask) instead of discovering the loss after the reconnect settles.
4. **The queue re-arms its end sentinel** so a stream attempt that opens
   after the ingress already closed still terminates cleanly instead of
   hanging on an empty queue.

Docs: docs/06 §9 (STT row). Tests: tests/voice/test_worker.py drives
this through VoiceAgentWorker with a scripted FakeStt.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import AsyncGenerator, Callable
from typing import cast

from app.domain.voice import SttEvent, SttProvider
from app.obs.logging import get_logger
from app.voice.audio_ingress import STT_SAMPLE_RATE, AudioIngress

log = get_logger(__name__)

# Judgment call 2: consecutive zero-event deaths before giving up.
_MAX_DEATHS_WITHOUT_EVENTS = 2


class SttSupervisor:
    """Owns the STT transport for one call; forwards every event to
    `on_event` (the worker's routing) and reports stream deaths to
    `on_stream_died` (the worker fails its pending final-waiter there)."""

    def __init__(
        self,
        session_id: str,
        *,
        stt: SttProvider,
        ingress: AudioIngress,
        on_event: Callable[[SttEvent], None],
        on_stream_died: Callable[[], None],
    ) -> None:
        self._session_id = session_id
        self._stt = stt
        self._ingress = ingress
        self._on_event = on_event
        self._on_stream_died = on_stream_died
        self._audio_q: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def run(self) -> None:
        """Pump + stream until the ingress fan-out ends (call over) or
        the reconnect budget is exhausted (judgment call 2)."""
        pump = asyncio.create_task(
            self._pump_audio(), name=f"worker:stt-pump:{self._session_id}"
        )
        deaths_without_events = 0
        try:
            while True:
                yielded = False
                try:
                    stream = await self._open_stream()
                    async with contextlib.aclosing(stream):
                        async for event in stream:
                            yielded = True
                            self._on_event(event)
                    return  # clean close: ingress ended, the call is over
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._on_stream_died()  # judgment call 3
                    deaths_without_events = 0 if yielded else deaths_without_events + 1
                    if deaths_without_events >= _MAX_DEATHS_WITHOUT_EVENTS:
                        log.error(
                            "worker.stt_terminal", session_id=self._session_id, exc_info=True
                        )
                        return
                    log.warning(
                        "worker.stt_reconnecting", session_id=self._session_id, exc_info=True
                    )
        finally:
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)

    async def _pump_audio(self) -> None:
        """Judgment call 1: the single, call-long subscriber of the
        ingress STT fan-out."""
        try:
            async for chunk in self._ingress.stt_stream():
                self._audio_q.put_nowait(chunk)
        finally:
            self._audio_q.put_nowait(None)

    async def _queued_audio(self) -> AsyncGenerator[bytes, None]:
        """A fresh, disposable view over the audio queue per stream
        attempt (judgment call 1)."""
        while True:
            chunk = await self._audio_q.get()
            if chunk is None:
                self._audio_q.put_nowait(None)  # judgment call 4: re-arm
                return
            yield chunk

    async def _open_stream(self) -> AsyncGenerator[SttEvent, None]:
        """The interfaces.py Protocol wart (`async def ... ->
        AsyncIterator` reads as coroutine-returning while every real
        implementation is an async generator) — the same dual-shape
        handling as ConversationManager._open_stream."""
        raw: object = self._stt.stream(self._queued_audio(), sample_rate=STT_SAMPLE_RATE)
        if inspect.iscoroutine(raw):
            raw = await raw
        return cast("AsyncGenerator[SttEvent, None]", raw)


__all__ = ["SttSupervisor"]
