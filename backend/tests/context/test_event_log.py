"""`EventLog` — RPUSH + LTRIM append-only event timeline (docs/08
§4.2)."""

from __future__ import annotations

import pytest

from app.context.event_log import EVENTS_MAX_LEN, EventLog
from app.context.redis_keys import CTX_TTL_SECONDS
from tests.support.fake_redis import FakePipeline, FakeRedis


async def test_get_events_is_empty_when_session_absent(event_log: EventLog) -> None:
    assert await event_log.get_events("sess_1") == []


async def test_append_then_get_events_round_trips_in_order(event_log: EventLog) -> None:
    event_1 = {"type": "nav", "name": "PaymentScreen", "ts": 1, "from": "DashboardScreen"}
    event_2 = {"type": "tap", "name": "Pay Now", "ts": 2, "screen": "PaymentScreen"}

    await event_log.append("sess_1", event_1)
    await event_log.append("sess_1", event_2)

    assert await event_log.get_events("sess_1") == [event_1, event_2]


async def test_append_uses_the_canon_ctx_events_key(
    event_log: EventLog, fake_redis: FakeRedis
) -> None:
    await event_log.append("sess_1", {"type": "tap", "name": "x", "ts": 1})

    assert "ctx:sess_1:events" in fake_redis.lists
    assert "ctx:sess_1" not in fake_redis.lists


async def test_append_applies_the_60_minute_ctx_ttl(
    event_log: EventLog, fake_redis: FakeRedis
) -> None:
    await event_log.append("sess_1", {"type": "tap", "name": "x", "ts": 1})

    assert fake_redis.ttls["ctx:sess_1:events"] == CTX_TTL_SECONDS


async def test_append_trims_to_the_newest_200_entries(event_log: EventLog) -> None:
    for i in range(EVENTS_MAX_LEN + 10):
        await event_log.append("sess_1", {"type": "tap", "name": str(i), "ts": i})

    events = await event_log.get_events("sess_1")

    assert len(events) == EVENTS_MAX_LEN
    # Drop-oldest: the surviving window is the newest EVENTS_MAX_LEN, in order.
    assert events[0]["name"] == "10"
    assert events[-1]["name"] == str(EVENTS_MAX_LEN + 9)


async def test_append_does_not_trim_under_the_cap(event_log: EventLog) -> None:
    await event_log.append("sess_1", {"type": "tap", "name": "a", "ts": 1})
    await event_log.append("sess_1", {"type": "tap", "name": "b", "ts": 2})

    events = await event_log.get_events("sess_1")

    assert [e["name"] for e in events] == ["a", "b"]


async def test_get_events_with_limit_returns_only_the_newest_n(event_log: EventLog) -> None:
    for i in range(5):
        await event_log.append("sess_1", {"type": "tap", "name": str(i), "ts": i})

    newest_two = await event_log.get_events("sess_1", limit=2)

    assert [e["name"] for e in newest_two] == ["3", "4"]


async def test_get_events_with_limit_zero_returns_empty(event_log: EventLog) -> None:
    await event_log.append("sess_1", {"type": "tap", "name": "a", "ts": 1})

    assert await event_log.get_events("sess_1", limit=0) == []


async def test_get_events_with_limit_larger_than_stored_returns_everything(
    event_log: EventLog,
) -> None:
    await event_log.append("sess_1", {"type": "tap", "name": "a", "ts": 1})

    events = await event_log.get_events("sess_1", limit=200)

    assert [e["name"] for e in events] == ["a"]


async def test_events_are_scoped_per_session(event_log: EventLog) -> None:
    await event_log.append("sess_1", {"type": "tap", "name": "a", "ts": 1})
    await event_log.append("sess_2", {"type": "tap", "name": "b", "ts": 1})

    assert [e["name"] for e in await event_log.get_events("sess_1")] == ["a"]
    assert [e["name"] for e in await event_log.get_events("sess_2")] == ["b"]


async def test_append_rejects_a_colon_in_session_id(event_log: EventLog) -> None:
    with pytest.raises(ValueError, match="session_id"):
        await event_log.append("sess:evil", {"type": "tap", "name": "a", "ts": 1})


# -- append_many (audit fix, 2026-08-05: pipelined batch for POST /v1/sessions'
# recent_events, replacing an N-call loop over append) ---------------------


async def test_append_many_round_trips_in_order(event_log: EventLog) -> None:
    event_1 = {"type": "nav", "name": "PaymentScreen", "ts": 1, "from": "DashboardScreen"}
    event_2 = {"type": "tap", "name": "Pay Now", "ts": 2, "screen": "PaymentScreen"}

    await event_log.append_many("sess_1", [event_1, event_2])

    assert await event_log.get_events("sess_1") == [event_1, event_2]


async def test_append_many_queues_one_variadic_rpush_in_one_pipeline(
    event_log: EventLog, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of the fix: one `pipeline().execute()` carrying
    exactly three commands, the first a single variadic `RPUSH` holding
    every event -- not `len(events)` single-value ones.

    Asserting only the `pipeline()` call count is not enough, which the
    first version of this test got wrong. Verified 2026-08-05 by mutating
    `append_many` to queue a single-value `RPUSH` per event into the same
    pipeline: `pipeline()` is still called exactly once, so that weaker
    assertion stayed green while the regression it names was live -- as
    did every test in the two files covering this path (this one and
    `tests/api/test_session_routes.py`, 55 between them at the time).
    Asserting the queued command shape is what actually catches it."""
    real_pipeline = fake_redis.pipeline
    pipelines: list[FakePipeline] = []

    def _recording_pipeline(transaction: bool = True) -> FakePipeline:
        pipe = real_pipeline(transaction=transaction)
        pipelines.append(pipe)
        return pipe

    monkeypatch.setattr(fake_redis, "pipeline", _recording_pipeline)

    events = [{"type": "tap", "name": str(i), "ts": i} for i in range(5)]
    await event_log.append_many("sess_1", events)

    assert len(pipelines) == 1
    queued = pipelines[0].executed_ops
    assert [name for name, _ in queued] == ["rpush", "ltrim", "expire"]

    key, *pushed = queued[0][1]
    assert key == "ctx:sess_1:events"
    assert len(pushed) == len(events)


async def test_append_many_applies_the_ttl_and_trims_to_the_cap(
    event_log: EventLog, fake_redis: FakeRedis
) -> None:
    await event_log.append_many(
        "sess_1", [{"type": "tap", "name": str(i), "ts": i} for i in range(EVENTS_MAX_LEN + 10)]
    )

    events = await event_log.get_events("sess_1")

    assert len(events) == EVENTS_MAX_LEN
    assert events[0]["name"] == "10"
    assert fake_redis.ttls["ctx:sess_1:events"] == CTX_TTL_SECONDS


async def test_append_many_with_empty_list_writes_nothing(
    event_log: EventLog, fake_redis: FakeRedis
) -> None:
    """A no-op, deliberately: an empty `RPUSH` is invalid, and "nothing to
    persist" needs no round trip -- matches `_persist_recent_events`'s own
    early return for `recent_events: []`, which relies on this."""
    await event_log.append_many("sess_1", [])

    assert "ctx:sess_1:events" not in fake_redis.lists


async def test_append_many_survives_alongside_append_on_the_same_key(
    event_log: EventLog,
) -> None:
    """`ContextDispatcher` (in-call, one event at a time) and
    `_persist_recent_events` (REST, batched) both ultimately write through
    the same key shape -- confirms the two paths compose rather than
    stepping on each other's ordering."""
    await event_log.append("sess_1", {"type": "nav", "name": "first", "ts": 1})
    await event_log.append_many(
        "sess_1",
        [
            {"type": "tap", "name": "second", "ts": 2},
            {"type": "tap", "name": "third", "ts": 3},
        ],
    )

    events = await event_log.get_events("sess_1")
    assert [e["name"] for e in events] == ["first", "second", "third"]
