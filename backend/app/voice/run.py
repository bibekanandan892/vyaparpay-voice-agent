"""voice-worker entrypoint — `python -m app.voice.run`, exactly as
docker-compose.yml's voice-worker service invokes it (docs/04 §1's second
entrypoint): config, logging/tracing init, the `/v1/signal` signaling
server on the configured port, graceful shutdown on SIGTERM.

PLACEHOLDER SCOPE — read before extending. T3.1 ends at "media and
messages flow between a client and the worker's ingress/egress + data
channel" (docs/06 §1's transport layer). The conversation brain —
VoiceAgentWorker's turn machine, STT/TTS/LLM in the loop — is a LATER
task, so `PlaceholderCallSession` below terminates media honestly but
speaks only paced silence: uplink audio is decoded, resampled, and fanned
out by AudioIngress (its consumers drained by no-op tasks until the real
worker subscribes them), the downlink AudioStreamTrack is fed by a running
AudioEgress with nothing enqueued, and the `ctx` data channel opens and
ignores unknown types per docs/13 §9. Every piece of per-call wiring here
is exactly what VoiceAgentWorker will take over; only the drains and the
empty egress are placeholder.

Judgment calls, flagged per house style:

1. **No per-turn spans are opened here.** docs/04 §7.2's frozen span table
   is turn-scoped (`turn`, `stt.final`, ...), and no turn exists until the
   brain lands — `setup_observability` is initialized so the later task
   inherits a working provider, and structlog carries the connection
   lifecycle meanwhile.
2. **The worker's own ICE config is STUN-only via `COTURN_HOST`** (srflx,
   docs/06 §2's candidate table), empty → host-only. The worker never
   allocates TURN relay for itself: the client holds the HMAC relay
   credentials (docs/13 §7) and one relayed side is sufficient for the
   pair to connect — a worker-side allocation would need a second minted
   credential for zero additional connectivity in the compose topology.
3. **SIGTERM/SIGINT via loop signal handlers**, with a `signal.signal`
   fallback where the loop API is unavailable (Windows dev hosts); the
   container path (compose → Linux) always takes the loop route. Shutdown
   order: stop accepting, `close_all()` (bye + peer teardown per session),
   then close Redis.
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import AsyncIterator

from websockets.asyncio.server import serve

from app.config import Settings, get_settings
from app.data.redis_client import RedisClient
from app.domain.voice import IceServer
from app.obs.logging import configure_logging, get_logger
from app.obs.tracing import setup_observability
from app.voice.audio_egress import AudioEgress
from app.voice.audio_ingress import AudioIngress
from app.voice.peer_session import PeerSession, SendSignal
from app.voice.signaling import SIGNALING_PATH, SignalingServer

log = get_logger(__name__)

_CLOSE_TIMEOUT_S = 5.0


async def _drain(stream: AsyncIterator[object]) -> None:
    """No-op consumer for an ingress fan-out until VoiceAgentWorker (a
    later task) subscribes the real STT/VAD pipelines — without it the
    bounded queues would fill to their 30 s caps and warn-log on every
    frame (audio_ingress judgment call 4)."""
    async for _ in stream:
        pass


class PlaceholderCallSession:
    """Per-call wiring for T3.1: PeerSession + ingress/egress + tasks.

    Satisfies signaling's PeerSessionLike. VoiceAgentWorker replaces this
    class wholesale; see the module docstring for what is placeholder.
    """

    def __init__(
        self, session_id: str, send_signal: SendSignal, *, ice_servers: tuple[IceServer, ...]
    ) -> None:
        self._ingress = AudioIngress()
        # AudioEgress contract carried forward for the task that lands the
        # turn machine (review sharp edge): `flush()` deliberately does NOT
        # `resume()` — duck (pause) and commit (flush) are separate signals
        # (audio_egress judgment call 9, docs/06 §6.1), so VoiceAgentWorker
        # MUST call `resume()` after every committed flush before the next
        # reply's audio can play. Nothing here may hide or work around that.
        self._egress = AudioEgress()
        self._peer = PeerSession(
            session_id,
            ingress=self._ingress,
            send_signal=send_signal,
            ice_servers=ice_servers,
        )
        self._tasks = (
            asyncio.create_task(
                self._egress.run(self._peer.outbound_sink), name=f"egress-run:{session_id}"
            ),
            asyncio.create_task(
                _drain(self._ingress.stt_stream()), name=f"stt-drain:{session_id}"
            ),
            asyncio.create_task(
                _drain(self._ingress.vad_frames()), name=f"vad-drain:{session_id}"
            ),
        )

    async def handle_offer(self, sdp: str) -> None:
        await self._peer.handle_offer(sdp)

    async def handle_remote_ice(self, payload: object) -> None:
        await self._peer.handle_remote_ice(payload)  # type: ignore[arg-type]

    async def close(self) -> None:
        """Teardown without leaks: peer first (ends the uplink drain and
        closes ingress, which ends the fan-out drains), then stop the paced
        egress loop; anything still pending after the timeout is cancelled
        loudly rather than leaked silently."""
        await self._peer.close()
        self._egress.stop()
        done, pending = await asyncio.wait(self._tasks, timeout=_CLOSE_TIMEOUT_S)
        for task in pending:
            log.warning("voice_worker.call_task_cancelled", task=task.get_name())
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


def _worker_ice_servers(settings: Settings) -> tuple[IceServer, ...]:
    """Judgment call 2: coturn as STUN for srflx, or host-only."""
    if not settings.coturn_host:
        return ()
    return (IceServer(urls=(f"stun:{settings.coturn_host}:3478",)),)


def _install_signal_handlers(stop: asyncio.Event) -> None:
    """Judgment call 3: graceful-shutdown trigger for SIGTERM (compose
    stop) and SIGINT (local ctrl-C)."""
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:
            # Windows dev host: the loop API is unsupported; a plain
            # handler still flips the event on the next loop wakeup.
            signal.signal(signum, lambda *_: stop.set())


async def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    setup_observability(settings)
    redis = RedisClient.from_settings(settings)
    ice_servers = _worker_ice_servers(settings)

    def peer_factory(session_id: str, send_signal: SendSignal) -> PlaceholderCallSession:
        return PlaceholderCallSession(session_id, send_signal, ice_servers=ice_servers)

    server = SignalingServer(redis=redis, peer_factory=peer_factory)
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    try:
        async with serve(
            server.handle_websocket,
            settings.signaling_bind_host,
            settings.signaling_bind_port,
        ):
            log.info(
                "voice_worker.listening",
                host=settings.signaling_bind_host,
                port=settings.signaling_bind_port,
                path=SIGNALING_PATH,
            )
            await stop.wait()
            log.info("voice_worker.shutdown_started")
            await server.close_all(reason="agent_hangup")
    finally:
        await redis.close()
    log.info("voice_worker.stopped")


if __name__ == "__main__":
    asyncio.run(main())
