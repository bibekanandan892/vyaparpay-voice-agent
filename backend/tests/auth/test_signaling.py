"""Unit tests for app.auth.signaling — the one-time signaling token
(docs/13-api-contracts.md §6.2).

Redis semantics run against `tests/support/fake_redis.FakeRedis` wrapped in
a REAL `RedisClient`, the pattern that module's own docstring describes:
the key name, the TTL argument, and the `GETDEL` burn are all exercised
through production code, only the socket is fake.
"""

from __future__ import annotations

import hashlib

import pytest

from app.auth.signaling import (
    TOKEN_PREFIX,
    hash_signaling_token,
    mint_signaling_token,
    store_signaling_token,
    verify_and_burn,
)
from app.data.redis_client import RedisClient
from tests.support.fake_redis import FakeRedis

_SESSION_ID = "a1f3c9d0e2b4"
_TTL_S = 300

# 32 random bytes rendered as URL-safe base64 (design note 1) — 43 chars,
# no padding. Asserted as a floor, not an equality, so the test pins the
# security property (enough entropy) rather than the exact encoding.
_MIN_RANDOM_CHARS = 43


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def redis(fake_redis: FakeRedis) -> RedisClient:
    return RedisClient(fake_redis, session_ttl_seconds=86_400)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# mint / hash
# --------------------------------------------------------------------------


def test_minted_token_has_the_contract_prefix_and_enough_entropy() -> None:
    """`^st_` is the one part of the shape that IS a contract
    (protocol/schemas/session_create_response.v1.json)."""
    minted = mint_signaling_token()

    assert minted.token.startswith(TOKEN_PREFIX)
    assert len(minted.token) - len(TOKEN_PREFIX) >= _MIN_RANDOM_CHARS


def test_minted_tokens_are_unique() -> None:
    tokens = {mint_signaling_token().token for _ in range(50)}

    assert len(tokens) == 50


def test_token_hash_is_the_sha256_hex_digest_of_the_token() -> None:
    minted = mint_signaling_token()

    assert minted.token_hash == hashlib.sha256(minted.token.encode("utf-8")).hexdigest()
    assert minted.token_hash == hash_signaling_token(minted.token)
    assert len(minted.token_hash) == 64


def test_repr_never_leaks_the_plaintext_token() -> None:
    """`Field(repr=False)`, mirroring app/domain/voice.py's judgment call
    8: an f-string, a traceback frame, or a stray structlog kwarg must not
    print the bearer value."""
    minted = mint_signaling_token()

    assert minted.token not in repr(minted)
    assert minted.token not in f"{minted}"
    assert minted.token_hash in repr(minted)  # the digest is not a secret


# --------------------------------------------------------------------------
# store / verify-and-burn
# --------------------------------------------------------------------------


async def test_store_writes_only_the_digest_under_the_canon_key_with_its_ttl(
    redis: RedisClient, fake_redis: FakeRedis
) -> None:
    minted = mint_signaling_token()

    await store_signaling_token(redis, _SESSION_ID, minted.token_hash, ttl_s=_TTL_S)

    key = f"signal_token:{_SESSION_ID}"
    assert fake_redis.strings[key] == minted.token_hash
    assert fake_redis.ttls[key] == _TTL_S
    # The plaintext exists nowhere in Redis — the whole point of storing
    # a digest (design note 2).
    assert minted.token not in fake_redis.strings.values()


async def test_verify_and_burn_accepts_the_minted_token_once(redis: RedisClient) -> None:
    minted = mint_signaling_token()
    await store_signaling_token(redis, _SESSION_ID, minted.token_hash, ttl_s=_TTL_S)

    assert await verify_and_burn(redis, _SESSION_ID, minted.token) is True


async def test_a_second_burn_of_the_same_token_fails(
    redis: RedisClient, fake_redis: FakeRedis
) -> None:
    """The one-time property (docs/13 §6.2: "A leaked or logged URL
    replays as a dead token")."""
    minted = mint_signaling_token()
    await store_signaling_token(redis, _SESSION_ID, minted.token_hash, ttl_s=_TTL_S)

    first = await verify_and_burn(redis, _SESSION_ID, minted.token)
    second = await verify_and_burn(redis, _SESSION_ID, minted.token)

    assert first is True
    assert second is False
    assert f"signal_token:{_SESSION_ID}" not in fake_redis.strings


async def test_wrong_token_fails_and_still_burns_the_pending_one(
    redis: RedisClient, fake_redis: FakeRedis
) -> None:
    """Design note 4, asserted so the trade-off can't regress silently:
    `GETDEL` deletes unconditionally, so a wrong presentation destroys
    the pending token too. Chosen over compare-then-delete, which
    reintroduces the double-accept race."""
    minted = mint_signaling_token()
    await store_signaling_token(redis, _SESSION_ID, minted.token_hash, ttl_s=_TTL_S)

    assert await verify_and_burn(redis, _SESSION_ID, "st_not-the-right-token") is False
    assert f"signal_token:{_SESSION_ID}" not in fake_redis.strings
    assert await verify_and_burn(redis, _SESSION_ID, minted.token) is False


async def test_verify_on_an_unknown_session_is_a_plain_false(redis: RedisClient) -> None:
    """Absent key (never minted / already burned / TTL expired) is
    indistinguishable from a wrong token at this seam."""
    assert await verify_and_burn(redis, "never-minted", "st_whatever") is False


async def test_a_remint_invalidates_the_token_it_replaces(redis: RedisClient) -> None:
    """`POST /v1/sessions/{id}/token` overwrites the digest, so at most
    one token per session is live at any moment."""
    first = mint_signaling_token()
    await store_signaling_token(redis, _SESSION_ID, first.token_hash, ttl_s=_TTL_S)
    second = mint_signaling_token()
    await store_signaling_token(redis, _SESSION_ID, second.token_hash, ttl_s=_TTL_S)

    assert await verify_and_burn(redis, _SESSION_ID, first.token) is False


async def test_non_positive_ttl_is_refused(redis: RedisClient) -> None:
    """A token key with no TTL would outlive its own documented deadline;
    Redis rejects `EX 0` outright. Fail at the wrapper, loudly."""
    minted = mint_signaling_token()

    with pytest.raises(ValueError, match="ttl_s must be positive"):
        await store_signaling_token(redis, _SESSION_ID, minted.token_hash, ttl_s=0)


async def test_session_id_with_a_colon_is_rejected_before_touching_redis(
    redis: RedisClient,
) -> None:
    """`RedisClient`'s colon-delimiter guard applies to this namespace
    too — a crafted id must not splice an extra key segment."""
    minted = mint_signaling_token()

    with pytest.raises(ValueError, match="session_id"):
        await store_signaling_token(redis, "a1f3:c9", minted.token_hash, ttl_s=_TTL_S)
