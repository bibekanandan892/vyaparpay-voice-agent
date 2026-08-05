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
        # What the last `execute()` drained, retained so a test can assert
        # on the exact command shape that was pipelined -- one variadic
        # RPUSH vs. N single-value ones, say. `_ops` itself is cleared by
        # `execute()`, mirroring the real client's one-shot semantics.
        self.executed_ops: list[tuple[str, tuple[Any, ...]]] = []

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

    def rpush(self, key: str, *values: str) -> FakePipeline:
        # Audit fix, 2026-08-05: added alongside `ltrim` so `EventLog
        # .append_many`'s pipeline (RPUSH all events, LTRIM, EXPIRE) has
        # something to queue against -- this pipeline previously only ever
        # carried `enforce_rate`'s zset ops.
        self._ops.append(("rpush", (key, *values)))
        return self

    def ltrim(self, key: str, start: int, end: int) -> FakePipeline:
        self._ops.append(("ltrim", (key, start, end)))
        return self

    async def execute(self) -> list[Any]:
        """Fidelity gap worth knowing before writing a failure-injection
        test against this: real Redis `MULTI`/`EXEC` commits the queued
        commands as a unit server-side, whereas this dispatches them one
        at a time, so an injected mid-`execute` raise leaves the earlier
        commands' mutations behind. Fine for the ordering and
        command-shape assertions this fake exists for -- just not
        evidence about real transactional atomicity."""
        self.executed_ops = list(self._ops)
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

    async def hset(
        self,
        key: str,
        field: str | None = None,
        value: str | None = None,
        *,
        mapping: dict[str, str] | None = None,
    ) -> None:
        # `mapping=` arm (Phase-5 Batch 2b review): real `HSET key f1 v1 f2 v2`
        # sets every field in ONE command, which is what makes
        # `RedisClient.set_summary`'s summary/summary_thru_turn pair
        # untearable. redis-py spells that `hset(name, mapping={...})`, so the
        # fake needs the same shape or that call writes nothing. The
        # positional single-field form every other call site uses is
        # unchanged.
        target = self.hashes.setdefault(key, {})
        if field is not None:
            target[field] = value if value is not None else ""
        if mapping:
            target.update(mapping)

    async def hmget(self, key: str, fields: list[str]) -> list[str | None]:
        # One command reading N fields — the read side of the same
        # atomicity property: `get_summary` must not see fold N's text
        # beside fold N+1's boundary.
        stored = self.hashes.get(key, {})
        return [stored.get(field) for field in fields]

    async def hdel(self, key: str, field: str) -> None:
        self.hashes.get(key, {}).pop(field, None)

    async def expire(self, key: str, seconds: int) -> None:
        self.ttls[key] = seconds

    # -- list ----------------------------------------------------------
    async def rpush(self, key: str, *values: str) -> None:
        # Variadic (audit fix, 2026-08-05): real `RPUSH key v1 v2 ... vn` is
        # variadic, and `EventLog.append_many` pipelines exactly one such
        # call for a whole batch rather than N single-value RPUSHes -- the
        # fake needs the same shape or that pipeline breaks under every test
        # that exercises it. `EventLog.append`'s existing single-value call
        # site still works unchanged: one positional argument is a
        # one-element `values` tuple.
        self.lists.setdefault(key, []).extend(values)

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

    def _rpush(self, key: str, *values: str) -> int:
        target = self.lists.setdefault(key, [])
        target.extend(values)
        return len(target)

    def _ltrim(self, key: str, start: int, end: int) -> bool:
        items = self.lists.get(key, [])
        self.lists[key] = items[start:] if end == -1 else items[start : end + 1]
        return True

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        return FakePipeline(self)


__all__ = ["FakePipeline", "FakeRedis"]
