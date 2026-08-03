"""Unit tests for app.data.redis_client — pure in-process, no real Redis.

Mocked at the `redis.asyncio.Redis` client boundary (per the plan's
testing constraint for this task, which has no Docker/Redis available):
`_FakeRedis` (imported below as `tests.support.fake_redis.FakeRedis`) is
a hand-rolled, in-memory stand-in that implements just the subset of the
Redis command surface `RedisClient` and `enforce_rate` actually issue
(hash/list/string ops, plus a pipelined ZSET sequence), with
`decode_responses=True` semantics — everything stored and returned as
`str`, matching how `RedisClient.from_settings` constructs the real
client.

Promoted to `tests/support/fake_redis.py` by task 6.2 (the E2E
canonical-conversation test, which needs the identical real-shaped-Redis
double to construct a REAL `RedisClient` against) rather than
re-implemented a second time there — this module still owns and exercises
its behavior in detail; the local `_FakeRedis`/`_FakePipeline` aliases
below keep every reference in this file unchanged.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.api.errors import RateLimitedError
from app.data.redis_client import RedisClient, enforce_rate
from app.domain.types import PendingConfirm
from tests.support.fake_redis import FakePipeline as _FakePipeline  # noqa: F401
from tests.support.fake_redis import FakeRedis as _FakeRedis

_SESSION_TTL = 86400


@pytest.fixture
def fake_redis() -> _FakeRedis:
    return _FakeRedis()


@pytest.fixture
def client(fake_redis: _FakeRedis) -> RedisClient:
    return RedisClient(fake_redis, session_ttl_seconds=_SESSION_TTL)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# session:{id} hash — transcript window
# --------------------------------------------------------------------------


async def test_get_transcript_window_is_empty_when_session_absent(client: RedisClient) -> None:
    assert await client.get_transcript_window("sess_1") == []


async def test_set_then_get_transcript_window_round_trips(client: RedisClient) -> None:
    turns = [
        {"role": "user", "text": "My payment got declined", "ts": 1, "tok": 6},
        {"role": "agent", "text": "Let me check that for you.", "ts": 2, "tok": 7},
    ]

    await client.set_transcript_window("sess_1", turns)

    assert await client.get_transcript_window("sess_1") == turns


async def test_set_transcript_window_applies_session_ttl(
    client: RedisClient, fake_redis: _FakeRedis
) -> None:
    await client.set_transcript_window("sess_1", [])

    assert fake_redis.ttls["session:sess_1"] == _SESSION_TTL


# --------------------------------------------------------------------------
# session:{id} hash — pending_confirm
# --------------------------------------------------------------------------


async def test_get_pending_confirm_is_none_when_absent(client: RedisClient) -> None:
    assert await client.get_pending_confirm("sess_1") is None


async def test_set_then_get_pending_confirm_round_trips(client: RedisClient) -> None:
    pending = PendingConfirm(
        tool="request_limit_increase",
        args={"new_limit": 50000},
        proposed_turn=5,
        invocation_id="inv_001",
    )

    await client.set_pending_confirm("sess_1", pending)
    fetched = await client.get_pending_confirm("sess_1")

    assert fetched == pending


async def test_set_pending_confirm_none_clears_the_field(client: RedisClient) -> None:
    pending = PendingConfirm(
        tool="request_limit_increase", args={}, proposed_turn=5, invocation_id="inv_001"
    )
    await client.set_pending_confirm("sess_1", pending)

    await client.set_pending_confirm("sess_1", None)

    assert await client.get_pending_confirm("sess_1") is None


async def test_set_pending_confirm_applies_session_ttl(
    client: RedisClient, fake_redis: _FakeRedis
) -> None:
    pending = PendingConfirm(
        tool="request_limit_increase", args={}, proposed_turn=1, invocation_id="inv_001"
    )

    await client.set_pending_confirm("sess_1", pending)

    assert fake_redis.ttls["session:sess_1"] == _SESSION_TTL


# --------------------------------------------------------------------------
# session:{id} hash — running cost
# --------------------------------------------------------------------------


async def test_get_running_cost_defaults_to_zero(client: RedisClient) -> None:
    assert await client.get_running_cost("sess_1") == Decimal("0")


async def test_set_then_get_running_cost_round_trips_as_decimal(client: RedisClient) -> None:
    await client.set_running_cost("sess_1", Decimal("0.1420"))

    assert await client.get_running_cost("sess_1") == Decimal("0.1420")


# --------------------------------------------------------------------------
# session:{id}:turns list
# --------------------------------------------------------------------------


async def test_get_turns_is_empty_when_session_absent(client: RedisClient) -> None:
    assert await client.get_turns("sess_1") == []


async def test_append_turn_then_get_turns_round_trips_in_order(client: RedisClient) -> None:
    turn_1 = {"turn_no": 1, "role": "agent", "latency_ms": 900, "tokens": 120, "cost": 0.01}
    turn_2 = {"turn_no": 2, "role": "user", "latency_ms": 50, "tokens": 10, "cost": 0.0}

    await client.append_turn("sess_1", turn_1)
    await client.append_turn("sess_1", turn_2)

    assert await client.get_turns("sess_1") == [turn_1, turn_2]


async def test_append_turn_applies_session_ttl(
    client: RedisClient, fake_redis: _FakeRedis
) -> None:
    await client.append_turn("sess_1", {"turn_no": 1})

    assert fake_redis.ttls["session:sess_1:turns"] == _SESSION_TTL


async def test_transcript_window_and_turns_list_use_independent_keys(
    client: RedisClient, fake_redis: _FakeRedis
) -> None:
    """The two `session:{id}` shapes (docs/12 §7's hash vs. its sibling
    list key) must never collide."""
    await client.set_transcript_window("sess_1", [{"role": "user", "text": "hi"}])
    await client.append_turn("sess_1", {"turn_no": 1})

    assert "session:sess_1" in fake_redis.hashes
    assert "session:sess_1:turns" in fake_redis.lists
    assert "session:sess_1" not in fake_redis.lists


# --------------------------------------------------------------------------
# idempotency:{key} string
# --------------------------------------------------------------------------


async def test_get_idempotency_is_none_when_absent(client: RedisClient) -> None:
    assert await client.get_idempotency("sess_1", "request_limit_increase", 5) is None


async def test_set_then_get_idempotency_round_trips(client: RedisClient) -> None:
    await client.set_idempotency("sess_1", "request_limit_increase", 5, "LMT-0001")

    fetched = await client.get_idempotency("sess_1", "request_limit_increase", 5)

    assert fetched == "LMT-0001"


async def test_idempotency_key_format_matches_session_tool_turn(
    client: RedisClient, fake_redis: _FakeRedis
) -> None:
    """Key format is pinned literally by the plan:
    f"{session_id}:{tool_name}:{turn_no}", namespaced under "idempotency:".
    """
    await client.set_idempotency("sess_1", "request_limit_increase", 5, "LMT-0001")

    assert fake_redis.strings == {"idempotency:sess_1:request_limit_increase:5": "LMT-0001"}


async def test_set_idempotency_applies_24h_ttl(
    client: RedisClient, fake_redis: _FakeRedis
) -> None:
    await client.set_idempotency("sess_1", "request_limit_increase", 5, "LMT-0001")

    assert fake_redis.ttls["idempotency:sess_1:request_limit_increase:5"] == 24 * 60 * 60


# --------------------------------------------------------------------------
# enforce_rate — sliding-window ZSET rate limiter (docs/04 §6)
# --------------------------------------------------------------------------


async def test_enforce_rate_allows_calls_under_the_limit(fake_redis: _FakeRedis) -> None:
    for _ in range(5):
        await enforce_rate(fake_redis, "usr_rajesh01", limit=5)  # type: ignore[arg-type]

    assert fake_redis._zcard("rate:usr_rajesh01") == 5


async def test_enforce_rate_raises_rate_limited_once_over_the_limit(
    fake_redis: _FakeRedis,
) -> None:
    for _ in range(5):
        await enforce_rate(fake_redis, "usr_rajesh01", limit=5)  # type: ignore[arg-type]

    with pytest.raises(RateLimitedError) as exc_info:
        await enforce_rate(fake_redis, "usr_rajesh01", limit=5)  # type: ignore[arg-type]

    assert exc_info.value.retry_after == 60
    assert exc_info.value.status_code == 429
    assert exc_info.value.code == "RATE_LIMITED"


async def test_enforce_rate_uses_the_canon_rate_key(fake_redis: _FakeRedis) -> None:
    await enforce_rate(fake_redis, "usr_rajesh01", limit=5)  # type: ignore[arg-type]

    assert list(fake_redis.zsets.keys()) == ["rate:usr_rajesh01"]


async def test_enforce_rate_evicts_entries_outside_the_window(
    monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
) -> None:
    """The sliding-window guarantee: a hit far enough in the past must not
    keep counting against a caller forever."""
    import app.data.redis_client as redis_client_module

    clock = {"now": 1_000.0}
    monkeypatch.setattr(redis_client_module.time, "time", lambda: clock["now"])

    await enforce_rate(fake_redis, "usr_rajesh01", limit=1, window_s=60)  # type: ignore[arg-type]

    clock["now"] = 1_100.0  # 100s later, outside the 60s window
    await enforce_rate(fake_redis, "usr_rajesh01", limit=1, window_s=60)  # type: ignore[arg-type]

    assert fake_redis._zcard("rate:usr_rajesh01") == 1


async def test_enforce_rate_is_scoped_per_user(fake_redis: _FakeRedis) -> None:
    for _ in range(5):
        await enforce_rate(fake_redis, "usr_rajesh01", limit=5)  # type: ignore[arg-type]

    await enforce_rate(fake_redis, "usr_kumar02", limit=5)  # type: ignore[arg-type]

    assert fake_redis._zcard("rate:usr_kumar02") == 1


# --------------------------------------------------------------------------
# Key-delimiter rejection (review finding: an unvalidated `:` in a
# session_id/tool_name/user_id could splice an extra key segment and
# collide across namespaces — tool_name in particular traces back to
# unconstrained LLM tool-call output with nothing upstream validating it
# yet).
# --------------------------------------------------------------------------


async def test_session_key_rejects_a_colon_in_session_id(client: RedisClient) -> None:
    with pytest.raises(ValueError, match="session_id"):
        await client.get_transcript_window("sess:evil")


async def test_idempotency_key_rejects_a_colon_in_tool_name(client: RedisClient) -> None:
    with pytest.raises(ValueError, match="tool_name"):
        await client.get_idempotency("sess_1", "get_wallet_balance:extra_segment", 3)


async def test_idempotency_key_rejects_a_colon_in_session_id(client: RedisClient) -> None:
    with pytest.raises(ValueError, match="session_id"):
        await client.set_idempotency("sess:evil", "get_wallet_balance", 3, "value")


async def test_enforce_rate_rejects_a_colon_in_user_id(fake_redis: _FakeRedis) -> None:
    with pytest.raises(ValueError, match="user_id"):
        await enforce_rate(fake_redis, "usr:evil", limit=5)  # type: ignore[arg-type]


async def test_signal_token_key_rejects_a_colon_in_session_id(client: RedisClient) -> None:
    with pytest.raises(ValueError, match="session_id"):
        await client.take_signaling_token_hash("sess:evil")


async def test_session_control_channel_rejects_a_colon_in_session_id(
    client: RedisClient,
) -> None:
    with pytest.raises(ValueError, match="session_id"):
        await client.publish_session_control("sess:evil", "end")


# --------------------------------------------------------------------------
# session_control:{id} pub/sub (docs/13 §2.2's explicit hang-up)
# --------------------------------------------------------------------------


async def test_publish_session_control_uses_the_canon_channel_and_returns_subscribers(
    client: RedisClient, fake_redis: _FakeRedis
) -> None:
    fake_redis.subscribers = 1

    delivered = await client.publish_session_control("sess_1", "end")

    assert delivered == 1
    assert fake_redis.published == [("session_control:sess_1", "end")]


async def test_publish_session_control_with_no_listener_is_not_an_error(
    client: RedisClient, fake_redis: _FakeRedis
) -> None:
    """Pub/sub has no persistence: a worker that already tore the call
    down is the normal reason nobody is listening, so 0 is a value the
    caller logs, not an exception this wrapper raises."""
    assert await client.publish_session_control("sess_1", "end") == 0
    assert fake_redis.published == [("session_control:sess_1", "end")]


# --------------------------------------------------------------------------
# session:{id}:turns defensive size cap (review finding: no bound on
# growth beyond the 24h session TTL — a stuck retry loop or an abusively
# long-lived session could grow this list unbounded).
# --------------------------------------------------------------------------


async def test_append_turn_trims_to_the_defensive_cap(client: RedisClient) -> None:
    from app.data.redis_client import _MAX_TURNS_PER_SESSION

    for i in range(_MAX_TURNS_PER_SESSION + 10):
        await client.append_turn("sess_1", {"turn_no": i})

    turns = await client.get_turns("sess_1")
    assert len(turns) == _MAX_TURNS_PER_SESSION
    # oldest entries are the ones dropped, not the newest — a post-call
    # drain reading this list must not lose the most recent turns.
    assert turns[-1] == {"turn_no": _MAX_TURNS_PER_SESSION + 9}
    assert turns[0] == {"turn_no": 10}


async def test_append_turn_does_not_trim_under_the_cap(client: RedisClient) -> None:
    await client.append_turn("sess_1", {"turn_no": 1})
    await client.append_turn("sess_1", {"turn_no": 2})

    turns = await client.get_turns("sess_1")
    assert turns == [{"turn_no": 1}, {"turn_no": 2}]


# --------------------------------------------------------------------------
# RedisClient.from_settings — the one construction path not previously
# covered (review LOW finding), now also pinning the redis_url SecretStr
# unwrap (review MEDIUM finding: redis_url was a plain str, inconsistent
# with database_url/openrouter_api_key's masking).
# --------------------------------------------------------------------------


def test_from_settings_unwraps_the_redis_url_secret() -> None:
    from app.config import Settings

    settings = Settings(
        jwt_secret="test-secret",
        database_url="postgresql+asyncpg://u:p@localhost/test",
        openrouter_api_key="test-key",
        redis_url="redis://localhost:6379/0",
    )  # type: ignore[arg-type]

    redis_client = RedisClient.from_settings(settings)

    # `.raw` is the escape hatch to the underlying client (see its own
    # docstring) — used here only to confirm from_settings actually built
    # a real client against the unwrapped URL, not the literal
    # "**********" a missed .get_secret_value() would produce.
    assert str(redis_client.raw.get_connection_kwargs()["host"]) == "localhost"
