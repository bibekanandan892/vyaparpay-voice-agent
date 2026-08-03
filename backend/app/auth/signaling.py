"""The one-time signaling token (docs/13-api-contracts.md §6.2): minted by
agent-api at `POST /v1/sessions` (and re-minted at
`POST /v1/sessions/{id}/token`), verified and burned by the voice-worker's
`SignalingServer` at WebSocket accept.

**Ordering invariant — the reason this module exists as its own file.**
docs/13 §6.2 states it as a security invariant, not a style choice:

    token checked -> token burned -> only then is SDP parsed

`verify_and_burn()` below is the whole of the first two steps and it is
atomic (one Redis `GETDEL`), so a WS accept handler cannot accidentally
implement "check, parse the offer, burn later" — there is no `verify()`
without the burn to call. The voice-worker task that lands
`SignalingServer` MUST await this function and act on a `False` return
(close with `AUTH_INVALID_TOKEN`, docs/13 §6) *before* it touches the
first `offer` frame: an unauthenticated peer never reaches the SDP parser.
This is documented here, ahead of that code existing, precisely so the
ordering is a property of the API rather than a review note someone has to
remember.

Design notes, recorded so review argues with the reasoning:

1. **Entropy: `secrets.token_urlsafe(32)` — 32 random bytes / 256 bits**,
   rendered as 43 URL-safe base64 characters after the `st_` prefix (46
   chars total). Reasoning: this is a pure bearer credential with no
   structure to verify offline, so its only defense is guess-resistance,
   and it travels in a WebSocket query string (docs/13 §6) where it must
   stay URL-safe. 256 bits is chosen to match the strength of the
   SHA-256 digest that is actually stored — a longer token would not
   make the stored side any harder to attack, and a shorter one (the
   common 128-bit floor) would leave the token, not the hash, as the
   weakest link. docs/13 §2.1's worked example
   (`st_hV4nQ9pXzL2mR8cT0kWyB6uJ`, 24 chars ≈ 144 bits) is an example,
   not a contract: `protocol/schemas/session_create_response.v1.json`
   pins only the `^st_` prefix, and §9 makes response *values* free as
   long as the shape holds.
2. **Only the SHA-256 hex digest is ever stored**, in Redis under
   `signal_token:{session_id}` with the key's own TTL as the deadline
   (docs/13 §6.2: "the deadline is structural, not procedural"). A Redis
   dump, an `INFO keyspace` scrape, or a replica leak yields hashes, not
   usable tokens. No salt/KDF: the input is 256 bits of uniform
   randomness, so there is no dictionary to precompute and a
   password-style KDF would only add latency to the connect path.
3. **The plaintext is returned to the caller exactly once** and never
   read back — `POST /v1/sessions` puts it in the response body and
   forgets it. There is deliberately no "fetch the current token"
   accessor anywhere in this module.
4. **A failed verification burns the key too.** `GETDEL` deletes
   unconditionally, so presenting a *wrong* token also destroys the
   pending one. This is chosen over a compare-then-delete: any
   check-then-act split reintroduces the race where two concurrent
   correct presentations both win, which is the exact property "one-time"
   exists to prevent. The cost is that an attacker who already knows a
   session id can burn its unused token — a nuisance whose recovery path
   (`POST /v1/sessions/{id}/token`, docs/13 §6.1) the client already
   implements for network changes.
5. **Nothing here logs.** No token, no digest, no session-scoped secret
   material reaches structlog from this module — callers log outcomes
   (`signaling_token_rejected` and friends), never inputs.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from app.data.redis_client import RedisClient

# docs/13 §6.2 / protocol/schemas/session_create_response.v1.json's
# `"pattern": "^st_"` — the one part of the token's shape that IS a
# contract.
TOKEN_PREFIX: Final = "st_"

# Design note 1: 32 bytes = 256 bits, matching the SHA-256 digest stored
# for it. `secrets.token_urlsafe` renders this as 43 URL-safe characters.
_TOKEN_ENTROPY_BYTES: Final = 32


class SignalingToken(BaseModel):
    """A freshly minted token and the digest that will be stored for it.

    `token` is the cleartext that goes in the `POST /v1/sessions`
    response body exactly once (design note 3) — `Field(repr=False)` so
    an f-string, a `repr()` in a traceback, or a stray
    `log.info("...", minted=minted)` can never print it, the same
    discipline `app/domain/voice.py`'s `SessionCredentials.signaling_token`
    applies (its judgment call 8). `token_hash` is what callers persist:
    the `conversations.signaling_token_hash` column and the
    `signal_token:{id}` Redis key.
    """

    model_config = ConfigDict(frozen=True)

    token: str = Field(repr=False)
    token_hash: str


def hash_signaling_token(token: str) -> str:
    """SHA-256 hex digest — the only form of the token that is ever
    written down (design note 2)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint_signaling_token() -> SignalingToken:
    """One fresh token + its digest. Pure: no Redis, no clock, no
    session id — the token is independent of the session it will be
    stored under, which is what lets `POST /v1/sessions` mint it *before*
    `SessionManager.create()` needs a digest for the NOT NULL column."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(_TOKEN_ENTROPY_BYTES)
    return SignalingToken(token=token, token_hash=hash_signaling_token(token))


async def store_signaling_token(
    redis: RedisClient, session_id: str, token_hash: str, *, ttl_s: int
) -> None:
    """Write the digest under `signal_token:{session_id}` with `ttl_s` as
    the connect deadline (docs/13 §6.2's 5 minutes, from
    `Settings.signaling_token_ttl_s`).

    Re-minting for a live session (docs/13 §2.1's reconnect endpoint)
    overwrites: at most one token is valid per session at any moment, so
    a re-mint implicitly invalidates the token it replaces. The key name
    itself is `RedisClient`'s to own (that module's "callers never
    assemble key strings by hand" rule), which is why this delegates
    instead of formatting the key here.
    """
    await redis.set_signaling_token_hash(session_id, token_hash, ttl_s=ttl_s)


async def verify_and_burn(redis: RedisClient, session_id: str, presented_token: str) -> bool:
    """Atomically consume the pending token for `session_id` and report
    whether `presented_token` was it.

    One `GETDEL` round trip: after this returns, the stored digest is
    gone whatever the verdict (design note 4), so a replayed URL — logged,
    screenshotted, or sniffed — verifies exactly zero more times. An
    absent key (never minted, already burned, or past its TTL) is an
    ordinary `False`, deliberately indistinguishable from a wrong token
    at this seam: the caller renders one `AUTH_INVALID_TOKEN` for all of
    them (docs/13 §6) rather than telling an attacker which of "no such
    session" and "wrong secret" they hit.

    `hmac.compare_digest`, not `==`: both operands are hex digests of
    public-ish length, so the timing channel is thin, but constant-time
    comparison of an attacker-supplied value against a stored secret is
    the default this codebase should never have to argue about at a call
    site.
    """
    stored_hash = await redis.take_signaling_token_hash(session_id)
    if stored_hash is None:
        return False
    return hmac.compare_digest(stored_hash, hash_signaling_token(presented_token))


__all__ = [
    "TOKEN_PREFIX",
    "SignalingToken",
    "hash_signaling_token",
    "mint_signaling_token",
    "store_signaling_token",
    "verify_and_burn",
]
