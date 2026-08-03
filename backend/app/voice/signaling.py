"""SignalingServer — the voice-worker's `/v1/signal` WebSocket logic
(docs/13 §6, docs/06 §2): verify-and-burn the one-time token, then route
`{v, type, payload}` envelopes (offer/answer/ice/bye/ping/pong) between the
client and the PeerSession that owns the aiortc peer.

Judgment calls, flagged per house style:

1. **Transport framework: `websockets`, behind a Protocol seam.** Neither
   docs/06 nor docs/04/13 mandates a WS server library (they name only the
   route and the envelope). aiortc's ecosystem examples default to aiohttp,
   but aiohttp is not a dependency of this repo, while `websockets>=17` is
   already pinned in the `voice` extra (backend/pyproject.toml) — and the
   worker serves nothing but this one WebSocket, so an HTTP framework buys
   nothing. This module stays transport-agnostic regardless: all logic
   runs against the minimal `SignalingTransport` Protocol (async-iterate
   incoming text frames, `send`, `close`), which `websockets`'
   ServerConnection satisfies structurally; only `handle_websocket()` even
   assumes a websockets-shaped `request.path`, and it is duck-typed so this
   module imports neither websockets nor aiortc.
2. **The peer factory is injected, and this module never imports
   PeerSession.** Two reasons: the docs/06 §1.1 confinement rule keeps
   aiortc knowledge in exactly one module, and the unit tests for this
   server must run without aiortc installed (only tests that *touch*
   aiortc importorskip it). run.py wires the real factory.
3. **Verify-and-burn happens exactly once per connection attempt, before
   any frame is read** (docs/13 §6.2's ordering invariant — an
   unauthenticated peer never reaches the SDP parser). An absent
   session_id/token short-circuits with AUTH_MISSING_TOKEN *without*
   touching Redis, so a malformed dial cannot burn a session's pending
   token. A wrong token burns (auth/signaling.py design note 4) and
   renders the same AUTH_INVALID_TOKEN as never-minted/expired. The
   plaintext token is never logged — not on success, not on failure.
4. **Single active connection per session**: a second *verified*
   connection while one is live is rejected with `SESSION_ALREADY_ACTIVE`
   — a new code inside the SESSION_ prefix class, which docs/13 §1.1/§9
   explicitly allow additively. Superseding (WS reconnect onto a live
   session, ICE restart, the 30 s grace timer) is docs/06 §8's
   reconnection layer, deferred to that task on purpose — rejecting is
   the only behavior that cannot corrupt a live call meanwhile.
5. **A malformed frame gets an `error` envelope and the connection
   lives.** The WS is a call's only control plane; killing it for one
   garbled frame would take down signaling for media that is still
   flowing (docs/13 §6.1: the WS outlives even its own usefulness
   gaps). Same treatment for a valid envelope whose type is
   server→client only (`answer`), except a client-sent `error`, which is
   logged and ignored — replying to an error with an error invites
   ping-pong.
6. **Unknown envelope types are ignored with a debug log** — docs/13 §9's
   forward-compatibility rule, and the reason `SignalMessage.type` is an
   open `str` (domain/voice.py judgment call 4). Ignoring is mandatory,
   not defensive style: a future additive type must not error.
7. **Keepalive is client-driven**: the server answers `ping` with a `pong`
   echoing `ts` (docs/13 §6's examples) and never initiates pings — the
   §6.1 sequence puts the 10 s loop on the client, and the server's
   liveness signal is the connection itself.
8. **Every teardown path converges**: client `bye`, transport EOF,
   transport error, and server shutdown all run the same finally block —
   unregister, `peer.close()`, close the transport. `close_all()` (run.py
   SIGTERM) additionally sends a `bye {reason: agent_hangup}` first so a
   live client learns the call ended cleanly (docs/13 §6's bye is
   bidirectional).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol
from urllib.parse import parse_qs, urlparse

import orjson
from pydantic import ValidationError

from app.auth.signaling import verify_and_burn
from app.data.redis_client import RedisClient
from app.domain.voice import (
    SIG_TYPE_ANSWER,
    SIG_TYPE_BYE,
    SIG_TYPE_ERROR,
    SIG_TYPE_ICE,
    SIG_TYPE_OFFER,
    SIG_TYPE_PING,
    SIG_TYPE_PONG,
    WIRE_ENVELOPE_V,
    SignalMessage,
)
from app.obs.logging import get_logger

log = get_logger(__name__)


def _log_exception(event: str, **kw: object) -> None:
    """`log.exception()`, but a rendering failure in the logging pipeline
    itself (e.g. a console renderer that can't encode a traceback's
    characters) can never take down the caller — every use here sits
    directly before the code that actually reports the failure to the
    client (`_send_error`/`transport.close`/returning from `_close_peer`),
    and a broken log line must not silently skip that."""
    try:
        log.exception(event, **kw)
    except Exception:
        try:
            log.error(f"{event}.log_failed", **kw)
        except Exception:
            pass  # the fallback itself must never be able to raise


# docs/13 §6: the one /v1 route that terminates on the voice-worker.
SIGNALING_PATH: Final = "/v1/signal"

# docs/13 §1.1 error vocabulary (same code strings as REST — signaling
# `error` payloads share it). SESSION_ALREADY_ACTIVE is additive within the
# SESSION_ prefix class per §1.1/§9 (judgment call 4).
CODE_AUTH_MISSING_TOKEN: Final = "AUTH_MISSING_TOKEN"
CODE_AUTH_INVALID_TOKEN: Final = "AUTH_INVALID_TOKEN"
CODE_VALIDATION_SCHEMA: Final = "VALIDATION_SCHEMA"
CODE_VALIDATION_UNSUPPORTED_VERSION: Final = "VALIDATION_UNSUPPORTED_VERSION"
CODE_SESSION_ALREADY_ACTIVE: Final = "SESSION_ALREADY_ACTIVE"
CODE_INTERNAL: Final = "INTERNAL"

# RFC 6455 close codes.
_WS_NORMAL: Final = 1000
_WS_GOING_AWAY: Final = 1001
_WS_POLICY_VIOLATION: Final = 1008
_WS_INTERNAL_ERROR: Final = 1011


class SignalingTransport(Protocol):
    """The minimal surface this server needs from a WebSocket connection —
    satisfied structurally by `websockets.asyncio.server.ServerConnection`
    and by the fake transport in tests (judgment call 1)."""

    def __aiter__(self) -> AsyncIterator[str | bytes]: ...

    async def send(self, message: str) -> None: ...

    async def close(self, code: int = 1000, reason: str = "") -> None: ...


class PeerSessionLike(Protocol):
    """What signaling needs from a peer session — PeerSession satisfies it,
    and fakes stand in without aiortc (judgment call 2)."""

    async def handle_offer(self, sdp: str) -> None: ...

    async def handle_remote_ice(self, payload: Mapping[str, Any]) -> None: ...

    async def close(self) -> None: ...


# Same shape as peer_session.SendSignal — duplicated on purpose, see that
# module's alias comment (importing it here would pull in aiortc).
SendSignal = Callable[[SignalMessage], Awaitable[None]]
PeerFactory = Callable[[str, SendSignal], PeerSessionLike]


@dataclass
class _ActiveCall:
    transport: SignalingTransport
    peer: PeerSessionLike


class SignalingServer:
    """Routes signaling envelopes for every live call on this worker.

    One instance per process; `handle_connection` runs once per accepted
    WebSocket, `close_all` on graceful shutdown (run.py).
    """

    def __init__(self, *, redis: RedisClient, peer_factory: PeerFactory) -> None:
        self._redis = redis
        self._peer_factory = peer_factory
        self._active: dict[str, _ActiveCall] = {}

    @property
    def active_sessions(self) -> tuple[str, ...]:
        return tuple(self._active)

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def handle_websocket(self, connection: Any) -> None:
        """websockets adapter: parse `/v1/signal?session_id=...&token=...`
        and delegate. Duck-typed (`connection.request.path`) so this module
        needs no websockets import; the query string is never logged — it
        carries the token."""
        parsed = urlparse(str(connection.request.path))
        if parsed.path != SIGNALING_PATH:
            log.warning("signaling.unknown_path_rejected", path=parsed.path)
            await connection.close(_WS_POLICY_VIOLATION, "unknown path")
            return
        params = parse_qs(parsed.query)
        session_id = (params.get("session_id") or [""])[0]
        token = (params.get("token") or [""])[0]
        await self.handle_connection(connection, session_id=session_id, token=token)

    async def handle_connection(
        self, transport: SignalingTransport, *, session_id: str, token: str
    ) -> None:
        """One accepted WebSocket, start to finish: authenticate (judgment
        call 3), instantiate the peer, route envelopes, tear down."""
        if not await self._authenticate(transport, session_id=session_id, token=token):
            return
        if session_id in self._active:
            log.warning("signaling.duplicate_connection_rejected", session_id=session_id)
            await self._send_error(
                transport,
                CODE_SESSION_ALREADY_ACTIVE,
                "A signaling connection for this session is already active.",
            )
            await transport.close(_WS_POLICY_VIOLATION, "session already active")
            return

        async def send_signal(message: SignalMessage) -> None:
            await transport.send(message.model_dump_json())

        try:
            peer = self._peer_factory(session_id, send_signal)
        except Exception:
            _log_exception("signaling.peer_setup_failed", session_id=session_id)
            await self._send_error(transport, CODE_INTERNAL, "Failed to set up call resources.")
            await transport.close(_WS_INTERNAL_ERROR, "peer setup failed")
            return
        self._active[session_id] = _ActiveCall(transport=transport, peer=peer)
        log.info("signaling.connected", session_id=session_id)
        try:
            await self._run_message_loop(transport, session_id, peer)
        except Exception as exc:
            # Abnormal transport closure is the mobile-network norm
            # (docs/06 §8) — note it, don't stack-trace it.
            log.warning(
                "signaling.connection_aborted", session_id=session_id, error=repr(exc)
            )
        finally:
            self._active.pop(session_id, None)
            await self._close_peer(peer, session_id)
            await self._close_transport(transport, _WS_NORMAL, "session closed")
            log.info("signaling.disconnected", session_id=session_id)

    async def _authenticate(
        self, transport: SignalingTransport, *, session_id: str, token: str
    ) -> bool:
        """Judgment call 3: missing credentials short-circuit without
        touching Redis; otherwise verify-and-burn exactly once."""
        if not session_id or not token:
            log.warning("signaling.missing_credentials", has_session_id=bool(session_id))
            await self._send_error(
                transport, CODE_AUTH_MISSING_TOKEN, "session_id and token are required."
            )
            await transport.close(_WS_POLICY_VIOLATION, "missing credentials")
            return False
        if not await verify_and_burn(self._redis, session_id, token):
            log.warning("signaling.token_rejected", session_id=session_id)
            await self._send_error(
                transport,
                CODE_AUTH_INVALID_TOKEN,
                "Signaling token invalid or expired. Re-mint via POST /v1/sessions/{id}/token.",
            )
            await transport.close(_WS_POLICY_VIOLATION, "invalid token")
            return False
        return True

    async def close_all(self, *, reason: str = "agent_hangup") -> None:
        """Graceful shutdown (judgment call 8): bye every live client, close
        every peer and transport. Safe to race the per-connection finally —
        both sides pop-then-close and `peer.close()` is idempotent."""
        for session_id, call in list(self._active.items()):
            self._active.pop(session_id, None)
            try:
                await self._send(call.transport, SignalMessage(
                    type=SIG_TYPE_BYE, payload={"reason": reason}
                ))
            except Exception:
                log.warning("signaling.shutdown_bye_failed", session_id=session_id)
            await self._close_peer(call.peer, session_id)
            await self._close_transport(call.transport, _WS_GOING_AWAY, "server shutting down")

    # ------------------------------------------------------------------
    # Message routing
    # ------------------------------------------------------------------

    async def _run_message_loop(
        self, transport: SignalingTransport, session_id: str, peer: PeerSessionLike
    ) -> None:
        async for raw in transport:
            if isinstance(raw, bytes | bytearray | memoryview):
                await self._send_error(
                    transport, CODE_VALIDATION_SCHEMA, "Signaling frames must be JSON text."
                )
                continue
            try:
                message = SignalMessage.model_validate(orjson.loads(raw))
            except (orjson.JSONDecodeError, ValidationError):
                log.warning("signaling.malformed_frame", session_id=session_id)
                await self._send_error(
                    transport, CODE_VALIDATION_SCHEMA, "Frame is not a valid signaling envelope."
                )
                continue
            if message.v != WIRE_ENVELOPE_V:
                await self._send_error(
                    transport,
                    CODE_VALIDATION_UNSUPPORTED_VERSION,
                    f"Unsupported envelope version {message.v}.",
                )
                continue
            if await self._dispatch(transport, session_id, peer, message):
                return  # bye — the caller's finally tears down

    async def _dispatch(
        self,
        transport: SignalingTransport,
        session_id: str,
        peer: PeerSessionLike,
        message: SignalMessage,
    ) -> bool:
        """Route one validated envelope. Returns True when the connection
        should end (bye)."""
        if message.type == SIG_TYPE_OFFER:
            await self._handle_offer(transport, session_id, peer, message.payload)
        elif message.type == SIG_TYPE_ICE:
            await self._handle_ice(transport, session_id, peer, message.payload)
        elif message.type == SIG_TYPE_PING:
            await self._handle_ping(transport, message.payload)
        elif message.type == SIG_TYPE_PONG:
            log.debug("signaling.pong_received", session_id=session_id)
        elif message.type == SIG_TYPE_BYE:
            log.info(
                "signaling.bye_received",
                session_id=session_id,
                reason=message.payload.get("reason"),
            )
            return True
        elif message.type == SIG_TYPE_ERROR:
            # Judgment call 5: log, never error back — no error ping-pong.
            log.warning(
                "signaling.client_error_received",
                session_id=session_id,
                code=message.payload.get("code"),
            )
        elif message.type == SIG_TYPE_ANSWER:
            await self._send_error(
                transport, CODE_VALIDATION_SCHEMA, "'answer' is server-to-client only."
            )
        else:
            # docs/13 §9: unknown types MUST be ignored (judgment call 6).
            log.debug("signaling.unknown_type_ignored", session_id=session_id, type=message.type)
        return False

    async def _handle_offer(
        self,
        transport: SignalingTransport,
        session_id: str,
        peer: PeerSessionLike,
        payload: Mapping[str, Any],
    ) -> None:
        sdp = payload.get("sdp")
        if not isinstance(sdp, str) or not sdp:
            await self._send_error(
                transport, CODE_VALIDATION_SCHEMA, "Offer payload requires a non-empty 'sdp'."
            )
            return
        try:
            await peer.handle_offer(sdp)
        except Exception:
            _log_exception("signaling.offer_failed", session_id=session_id)
            await self._send_error(transport, CODE_INTERNAL, "Failed to apply offer.")

    async def _handle_ice(
        self,
        transport: SignalingTransport,
        session_id: str,
        peer: PeerSessionLike,
        payload: Mapping[str, Any],
    ) -> None:
        if "candidate" not in payload:
            await self._send_error(
                transport, CODE_VALIDATION_SCHEMA, "Ice payload requires 'candidate'."
            )
            return
        # Shape-only checks duplicated from PeerSession.handle_remote_ice()
        # on purpose (same reasoning as the SendSignal alias above): these
        # are schema violations the client controls, not internal faults,
        # and CODE_VALIDATION_SCHEMA vs CODE_INTERNAL is a real distinction
        # docs/13 §1.1 draws for client-side error handling.
        candidate = payload["candidate"]
        if candidate is not None and (not isinstance(candidate, str) or not candidate):
            await self._send_error(
                transport,
                CODE_VALIDATION_SCHEMA,
                "Ice payload 'candidate' must be a non-empty string or null.",
            )
            return
        if candidate is not None and payload.get("sdpMid") is None and (
            payload.get("sdpMLineIndex") is None
        ):
            await self._send_error(
                transport,
                CODE_VALIDATION_SCHEMA,
                "Ice payload needs 'sdpMid' or 'sdpMLineIndex'.",
            )
            return
        try:
            await peer.handle_remote_ice(payload)
        except Exception:
            _log_exception("signaling.ice_failed", session_id=session_id)
            await self._send_error(transport, CODE_INTERNAL, "Failed to apply ICE candidate.")

    async def _handle_ping(
        self, transport: SignalingTransport, payload: Mapping[str, Any]
    ) -> None:
        ts = payload.get("ts")
        if not isinstance(ts, int) or isinstance(ts, bool):
            await self._send_error(
                transport, CODE_VALIDATION_SCHEMA, "Ping payload requires an integer 'ts'."
            )
            return
        await self._send(transport, SignalMessage(type=SIG_TYPE_PONG, payload={"ts": ts}))

    # ------------------------------------------------------------------
    # Send/teardown helpers
    # ------------------------------------------------------------------

    async def _send(self, transport: SignalingTransport, message: SignalMessage) -> None:
        await transport.send(message.model_dump_json())

    async def _send_error(
        self, transport: SignalingTransport, code: str, message: str
    ) -> None:
        try:
            await self._send(transport, SignalMessage(
                type=SIG_TYPE_ERROR, payload={"code": code, "message": message}
            ))
        except Exception:
            log.warning("signaling.error_send_failed", code=code)

    async def _close_peer(self, peer: PeerSessionLike, session_id: str) -> None:
        try:
            await peer.close()
        except Exception:
            _log_exception("signaling.peer_close_failed", session_id=session_id)

    async def _close_transport(
        self, transport: SignalingTransport, code: int, reason: str
    ) -> None:
        try:
            await transport.close(code, reason)
        except Exception:
            log.debug("signaling.transport_close_failed")


__all__ = [
    "CODE_AUTH_INVALID_TOKEN",
    "CODE_AUTH_MISSING_TOKEN",
    "CODE_INTERNAL",
    "CODE_SESSION_ALREADY_ACTIVE",
    "CODE_VALIDATION_SCHEMA",
    "CODE_VALIDATION_UNSUPPORTED_VERSION",
    "SIGNALING_PATH",
    "PeerFactory",
    "PeerSessionLike",
    "SendSignal",
    "SignalingServer",
    "SignalingTransport",
]
