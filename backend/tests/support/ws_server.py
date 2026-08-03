"""In-process WebSocket fake-server helper for the provider wire tests
(tests/providers/test_deepgram.py, tests/providers/test_elevenlabs.py) —
the WS analogue of the respx strategy docs/04 §9.4 fixes for HTTP
providers: NO live network, ever; each test scripts a handler that speaks
the vendor's documented wire protocol on 127.0.0.1 at an ephemeral port.

Kept deliberately tiny: tests own their handlers (and whatever recording
state those close over); this module only owns the serve-on-port-0 /
yield-the-url plumbing so it isn't reinvented per module (the same
promotion rule that moved FakeRedis into tests/support/fake_redis.py).

Import note: this module imports `websockets` (the [voice] extra), so any
test module importing it must sit behind the
`pytest.importorskip("websockets", ...)` guard first — the
tests/voice/test_peer_session.py convention.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from websockets.asyncio.server import ServerConnection, serve

WsHandler = Callable[[ServerConnection], Awaitable[None]]


@asynccontextmanager
async def ws_server(handler: WsHandler) -> AsyncIterator[str]:
    """Serve `handler` on 127.0.0.1:<ephemeral>; yields the `ws://host:port`
    base URL (no path — the client under test appends its own, and the
    handler can assert on `connection.request.path`)."""
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}"


__all__ = ["WsHandler", "ws_server"]
