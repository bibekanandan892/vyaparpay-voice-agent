"""`FakeRedis` — a hand-rolled, in-memory stand-in for `redis.asyncio.Redis`,
implementing just the subset of the Redis command surface `RedisClient`
(`app/data/redis_client.py`) and `enforce_rate` actually issue (hash/list/
string ops including `GETDEL`, a pub/sub `PUBLISH`, plus a pipelined ZSET
sequence), with `decode_responses=True` semantics — everything stored and
returned as `str`, matching how `RedisClient.from_settings` constructs the
real client.

Promoted here (task 6.2, the E2E canonical-conversation test) from its
original home as a private class in `tests/data/test_redis_client.py`
(task 2.1) — that module still owns and exercises this fake's behavior in
detail; this is the same implementation, reused rather than
re-implemented, so a REAL `RedisClient` can be constructed against a
REAL-shaped Redis surface in any test that needs one (unit tests wrapping
this directly, or an end-to-end test wiring the whole agent loop through
the real `RedisClient`/`SessionMemory` stack).
"""

from __future__ import annotations

from typing import Any


class FakePipeline:
    """Records queued ZSET ops and replays them against `FakeRedis` on
    `execute()`, in call order — mirrors the real `redis.asyncio` pipeline
    API closely enough for `enforce_rate`'s exact call sequence
    (zremrangebyscore, zadd, zcard, expire)."""

    def __init__(self, store: FakeRedis) -> None:
        self._store = store
        self._ops: list[tuple[str, tuple[Any, ...]]] = []

    def zremrangebyscore(self, key: str, min_: Any, max_: Any) -> FakePipeline:
        self._ops.append(("zremrangebyscore", (key, min_, max_)))
        return self

    def zadd(self, key: str, mapping: dict[str, float]) -> FakePipeline:
        self._ops.append(("zadd", (key, mapping)))
        return self

    def zcard(self, key: str) -> FakePipeline:
        self._ops.append(("zcard", (key,)))
        return self

    def expire(self, key: str, seconds: int) -> FakePipeline:
        self._ops.append(("expire", (key, seconds)))
        return self

    async def execute(self) -> list[Any]:
        results = [getattr(self._store, f"_{name}")(*args) for name, args in self._ops]
        self._ops.clear()
        return results

    async def __aenter__(self) -> FakePipeline:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class FakeRedis:
    """Minimal in-memory stand-in for `redis.asyncio.Redis`."""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.lists: dict[str, list[str]] = {}
        self.strings: dict[str, str] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.ttls: dict[str, int] = {}
        # (channel, message) in publish order — pub/sub has no server-side
        # storage, so this exists purely so a test can assert what was
        # published (e.g. DELETE /v1/sessions' "end" on
        # session_control:{id}). `subscribers` is what publish() reports
        # back; tests that care about the "nobody listening" path set it.
        self.published: list[tuple[str, str]] = []
        self.subscribers = 0

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

    async def getdel(self, key: str) -> str | None:
        """Real Redis `GETDEL`: return the value and delete the key in one
        atomic step. Single-threaded in-memory here, so "atomic" is free —
        what matters for the tests using it is the *semantics*, i.e. that a
        second call on the same key returns `None` (the one-time signaling
        token's burn, docs/13 §6.2)."""
        self.ttls.pop(key, None)
        return self.strings.pop(key, None)

    # -- pub/sub --------------------------------------------------------
    async def publish(self, channel: str, message: str) -> int:
        """Records the (channel, message) pair and returns the configured
        subscriber count, mirroring the real command's return value."""
        self.published.append((channel, message))
        return self.subscribers

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

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        return FakePipeline(self)


__all__ = ["FakePipeline", "FakeRedis"]
