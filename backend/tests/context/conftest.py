"""Shared fixtures for `backend/tests/context/` (Phase-4 T2: the context
ingestion pipeline, `backend/app/context/`).

Redis is faked, not containerized. `backend/tests/data/test_redis_client.py`
establishes the pattern this repo actually uses for every Redis-backed
component (`RedisClient`, and — per `backend/tests/e2e/
test_canonical_conversation.py` — even the one test that runs against a
REAL Postgres via `testcontainers` still fakes Redis): `RedisClient`
wrapping `tests/support/fake_redis.py`'s hand-rolled, in-memory `FakeRedis`.
`pyproject.toml`'s `dev` extra does carry `testcontainers[postgres,redis]`,
but grepping the suite turns up zero uses of the `redis` half of that
extra — no `RedisContainer` fixture exists anywhere in this repo to reuse.
Introducing one here, for a single task's tests, would be the odd one out
against every existing Redis-backed test (`test_redis_client.py`,
`test_session_manager.py`, the e2e canonical-conversation test) and adds a
Docker dependency this package's tests don't otherwise need. `FakeRedis`
already speaks the exact list/string/expire command surface
`SnapshotIngestor`/`EventLog` issue via `RedisClient.raw`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.context.context_compressor import ContextCompressor
from app.context.event_log import EventLog
from app.context.snapshot_ingestor import SnapshotIngestor
from app.data.redis_client import RedisClient
from tests.support.fake_redis import FakeRedis

_SESSION_TTL = 86400  # session:{id} TTL — irrelevant to ctx:* keys, but
# RedisClient's constructor requires a value.

_FIXTURES_DIR = Path(__file__).resolve().parents[3] / "protocol" / "fixtures" / "context"


def load_fixture(name: str) -> Any:
    """One `protocol/fixtures/context/{name}.json` fixture, the same
    frozen wire examples `tests/contract/test_context_protocol.py`
    validates — reused here rather than hand-rolling parallel example
    payloads."""
    return json.loads((_FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def redis_client(fake_redis: FakeRedis) -> RedisClient:
    return RedisClient(fake_redis, session_ttl_seconds=_SESSION_TTL)  # type: ignore[arg-type]


@pytest.fixture
def compressor() -> ContextCompressor:
    return ContextCompressor()


@pytest.fixture
def ingestor(redis_client: RedisClient, compressor: ContextCompressor) -> SnapshotIngestor:
    return SnapshotIngestor(redis_client, compressor)


@pytest.fixture
def event_log(redis_client: RedisClient) -> EventLog:
    return EventLog(redis_client)


__all__ = ["load_fixture"]
