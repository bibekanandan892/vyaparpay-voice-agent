"""Unit tests for app.data.redis_client — pure in-process, no real Redis.

Mocked at the `redis.asyncio.Redis` client boundary (per the plan's
testing constraint for this task, which has no Docker/Redis available):
`_FakeRedis` below is a hand-rolled, in-memory stand-in that implements
just the subset of the Redis command surface `RedisClient` and
`enforce_rate` actually issue (hash/list/string ops, plus a pipelined
ZSET sequence), with `decode_responses=True` semantics — everything
stored and returned as `str`, matching how `RedisClient.from_settings`
constructs the real client.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from app.api.errors import RateLimitedError
from app.data.redis_client import RedisClient, enforce_rate
from app.domain.types import PendingConfirm

_SESSION_TTL = 86400


class _FakePipeline:
    """Records queued ZSET ops and replays them against `_FakeRedis` on
    `execute()`, in call order — mirrors the real `redis.asyncio` pipeline
    API closely enough for `enforce_rate`'s exact call sequence
    (zremrangebyscore, zadd, zcard, expire)."""

    def __init__(self, store: _FakeRedis) -> None:
        self._store = store
        self._ops: list[tuple[str, tuple[Any, ...]]] = []

    def zremrangebyscore(self, key: str, min_: Any, max_: Any) -> _FakePipeline:
        self._ops.append(("zremrangebyscore", (key, min_, max_)))
        return self

    def zadd(self, key: str, mapping: dict[str, float]) -> _FakePipeline:
        self._ops.append(("zadd", (key, mapping)))
        return self

    def zcard(self, key: str) -> _FakePipeline:
        self._ops.append(("zcard", (key,)))
        return self

    def expire(self, key: str, seconds: int) -> _FakePipeline:
        self._ops.append(("expire", (key, seconds)))
        return self

    async def execute(self) -> list[Any]:
        results = [getattr(self._store, f"_{name}")(*args) for name, args in self._ops]
        self._ops.clear()
        return results

    async def __aenter__(self) -> _FakePipeline:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _FakeRedis:
    """Minimal in-memory stand-in for `redis.asyncio.Redis`."""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.lists: dict[str, list[str]] = {}
        self.strings: dict[str, str] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.ttls: dict[str, int] = {}

    # -- hash --------------------------------------------------------
    async def hget(self, key: str, field: str) -> str | None:
        return self.hashes.get(key, {}).get(field)

    async def hset(self, key: str, field: str, value: str) -> None:
        self.hashes.setdefault(key, {})[field] = value

    async def hdel(self, key: str, field: str) -> None:
        self.hashes.get(key, {}).pop(field, None)

    async def expire(self, key: str, seconds: int) -> None:
        self.ttls[key] = seconds

    # -- list ----------------------------------------------------------
    async def rpush(self, key: str, value: str) -> None:
        self.lists.setdefault(key, []).append(value)

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        items = self.lists.get(key, [])
        return items[start:] if end == -1 else items[start : end + 1]

    async def ltrim(self, key: str, start: int, end: int) -> None:
        items = self.lists.get(key, [])
        self.lists[key] = items[start:] if end == -1 else items[start : end + 1]

    # -- string --------------------------------------------------------
    async def get(self, key: str) -> str | None:
        return self.strings.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.strings[key] = value
        if ex is not None:
            self.ttls[key] = ex

    # -- zset (pipeline-only; mirrors the sync ops a real pipeline queues) --
    def _zremrangebyscore(self, key: str, min_: float, max_: float) -> int:
        zset = self.zsets.setdefault(key, {})
        stale = [member for member, score in zset.items() if min_ <= score <= max_]
        for member in stale:
            del zset[member]
        return len(stale)

    def _zadd(self, key: str, mapping: dict[str, float]) -> int:
        zset = self.zsets.setdefault(key, {})
        added = sum(1 for member in mapping if member not in zset)
        zset.update(mapping)
        return added

    def _zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))

    def _expire(self, key: str, seconds: int) -> bool:
        self.ttls[key] = seconds
        return True

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        return _FakePipeline(self)


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
