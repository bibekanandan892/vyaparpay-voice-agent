"""coturn `use-auth-secret` REST credentials (docs/13-api-contracts.md §7).

coturn runs with no user database: credentials are *computed* here at
session create and *recomputed* by coturn at allocation time from the same
shared `TURN_SECRET`. The scheme is the standard TURN REST-API credential
format:

    username   = "<expiry_unix_ts>:<session_id>"          -> "1784537062:a1f3c9"
    credential = base64( HMAC-SHA1( TURN_SECRET, username ) )

The expiry travels *inside* the signed username, so coturn rejects a stale
pair without any state of its own, and the session id makes relay usage
attributable in coturn's logs (docs/13 §7).

Notes recorded for review:

1. **HMAC-SHA1, not SHA-256.** Not a lapse: coturn's `use-auth-secret`
   implementation of the TURN REST API computes SHA-1 specifically, so
   any other digest simply fails to authenticate. The construction is an
   HMAC over a server-chosen message with a high-entropy shared secret —
   SHA-1's collision weakness does not apply to HMAC-SHA1 here — and the
   credential's blast radius is one 10-minute window (docs/13 §7's
   rotation row).
2. **10-minute TTL, longer than the 5-minute signaling token, on
   purpose** (docs/13 §7): the credential must survive mid-call
   allocation refreshes, not just the connect window. Both come from
   `Settings` (`turn_credential_ttl_s`, `signaling_token_ttl_s`), never
   from constants here.
3. **STUN entry first, credential-free; then one TURN entry carrying both
   URLs.** That is the exact `ice_servers` shape docs/13 §2.1's worked
   example shows and `protocol/schemas/session_create_response.v1.json`
   validates, and it is passed verbatim into `PeerConnection`
   construction on the client. `turn:…:3478?transport=udp` leads (the
   fast path) with `turns:…:5349` behind it as the TLS fallback for
   carrier NATs that eat UDP (docs/13 §7).
4. **Misconfiguration raises `RuntimeError`, not a typed `AppError`.**
   An agent-api booted without `TURN_SECRET`/`COTURN_HOST` cannot mint a
   working call for *anyone*; that is an operator error, not a
   client-facing business outcome. `ErrorEnvelopeMiddleware`
   (app/api/middleware.py) catches it, logs the traceback server-side and
   returns the generic `500 INTERNAL` body — the message below names the
   env var but never its value, and stays out of the response by policy
   (`InternalError`'s own docstring).
5. **`TURN_SECRET` is read via `.get_secret_value()` at the point of use
   only** (app/config.py's rule) and is never logged, never returned, and
   never placed in an exception message.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import UTC, datetime
from typing import Final

from app.config import Settings
from app.domain.voice import IceServer

# docs/13 §7 / §2.1's worked example: coturn's plain listener is 3478
# (STUN and TURN-over-UDP alike), its TLS listener 5349.
_STUN_PORT: Final = 3478
_TURN_UDP_PORT: Final = 3478
_TURNS_TLS_PORT: Final = 5349

# The username's two halves are joined by ':' — which also means a
# session id containing ':' would forge a different (expiry, session)
# split on coturn's side. Session ids are 12 lowercase hex chars
# (app/agent/session_manager.py's judgment call #1) so this can't happen
# today; the guard below keeps it true if that ever changes.
_USERNAME_SEPARATOR: Final = ":"


def build_turn_username(session_id: str, expiry_unix_ts: int) -> str:
    """docs/13 §7's `"<expiry_unix_ts>:<session_id>"`."""
    if _USERNAME_SEPARATOR in session_id:
        raise ValueError(f"session_id must not contain {_USERNAME_SEPARATOR!r}")
    return f"{expiry_unix_ts}{_USERNAME_SEPARATOR}{session_id}"


def compute_turn_credential(turn_secret: str, username: str) -> str:
    """`base64( HMAC-SHA1( TURN_SECRET, username ) )` — the exact bytes
    coturn recomputes and compares (note 1). Standard-library `hmac`/
    `hashlib`/`base64`, no vendor SDK."""
    digest = hmac.new(
        turn_secret.encode("utf-8"), username.encode("utf-8"), hashlib.sha1
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def _require_turn_config(settings: Settings) -> tuple[str, str]:
    """(secret, host) or `RuntimeError` — note 4. Returns the secret
    rather than storing it anywhere; the caller uses it once, immediately.
    """
    if settings.turn_secret is None:
        raise RuntimeError(
            "TURN_SECRET is not configured — agent-api cannot mint TURN credentials "
            "(docs/13 §7). Set it in the environment shared with coturn."
        )
    if not settings.coturn_host:
        raise RuntimeError(
            "COTURN_HOST is not configured — agent-api cannot build ice_servers URLs "
            "(docs/13 §2.1)."
        )
    return settings.turn_secret.get_secret_value(), settings.coturn_host


def mint_ice_servers(
    session_id: str, settings: Settings, *, now: datetime | None = None
) -> tuple[IceServer, ...]:
    """The `ice_servers` array of the connect bundle (docs/13 §2.1): the
    credential-free STUN entry, then the TURN entry with both URLs and
    the HMAC pair (note 3).

    `now` is injectable so tests can pin the expiry that ends up inside
    the signed username; production always passes nothing and gets
    `datetime.now(UTC)`.
    """
    turn_secret, host = _require_turn_config(settings)
    moment = now if now is not None else datetime.now(UTC)
    expiry_unix_ts = int(moment.timestamp()) + settings.turn_credential_ttl_s
    username = build_turn_username(session_id, expiry_unix_ts)
    return (
        IceServer(urls=(f"stun:{host}:{_STUN_PORT}",)),
        IceServer(
            urls=(
                f"turn:{host}:{_TURN_UDP_PORT}?transport=udp",
                f"turns:{host}:{_TURNS_TLS_PORT}",
            ),
            username=username,
            credential=compute_turn_credential(turn_secret, username),
        ),
    )


__all__ = ["build_turn_username", "compute_turn_credential", "mint_ice_servers"]
