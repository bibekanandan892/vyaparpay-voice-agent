"""Route-level tests for app/api/routes/sessions.py — the four session
endpoints (docs/13-api-contracts.md §2), driven through the full ASGI
stack (real `create_app()`, all four middleware layers, real routing) with
no Postgres and no real Redis.

Same hermetic strategy as tests/api/test_routes.py: `get_db` /
`require_rate_limit` are swapped via `app.dependency_overrides`, each
repository is monkeypatched where the route looks it up, and the Redis
side is a REAL `RedisClient` wrapped around `tests/support/fake_redis`'s
`FakeRedis` — so the token digest, its TTL, the `GETDEL` burn and the
`session_control:{id}` publish are all exercised through production code.

The `POST /v1/sessions` happy path additionally validates the live
response body against `protocol/schemas/session_create_response.v1.json`
using the hand-rolled walker tests/contract/test_voice_protocol.py already
owns (`protocol/` is the contract authority, docs/13 §8) — reusing that
validator rather than a second copy is the point: one schema, one
validator, two call sites (the frozen fixture, and what the route
actually emits).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient
from redis.exceptions import RedisError

from app.api.deps import get_db, require_rate_limit
from app.api.routes.sessions import (
    _MAX_RECENT_EVENTS,
    SESSION_CONTROL_END,
    get_redis,
    get_session_manager,
)
from app.auth.signaling import hash_signaling_token
from app.config import get_settings
from app.data.redis_client import RedisClient
from app.domain.types import Session, SessionState
from app.models import Conversation, ConversationTurn, ToolInvocation
from tests.context.conftest import load_fixture
from tests.contract.test_voice_protocol import _assert_valid, _load_fixture, _load_schema
from tests.support.fake_redis import FakeRedis

_JWT_SECRET = "test-jwt-secret"
_MERCHANT_ID = "usr_rajesh01"
_OTHER_MERCHANT_ID = "usr_someone_else"
_SESSION_ID = "a1f3c9d0e2b4"
_COTURN_HOST = "turn.vyapar.local"
_SIGNALING_URL = "wss://voice.vyapar.local/v1/signal"
_TURN_SECRET = "vyapar-turn-test-secret"
_SIGNALING_TTL_S = 300

_STARTED_AT = datetime(2026, 7, 24, 14, 14, 24, tzinfo=UTC)
_ENDED_AT = _STARTED_AT + timedelta(seconds=316)


def _token(sub: str = _MERCHANT_ID) -> str:
    return jwt.encode(
        {"sub": sub, "exp": datetime.now(UTC) + timedelta(hours=24)},
        _JWT_SECRET,
        algorithm="HS256",
    )


_AUTH_HEADERS = {"Authorization": f"Bearer {_token()}"}


class _SpySession:
    """Stands in for the request-scoped `AsyncSession`; the session routes
    never commit explicitly (all their writes go through SessionManager's
    own transaction or Redis), so this only has to exist."""

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


class _FakeSessionManager:
    """Records what `create()` was handed — above all the real signaling
    token digest, which is the whole point of the Batch-D change to
    `SessionManager.create()`."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(
        self,
        user_id: str,
        screen_context: dict[str, Any] | None,
        recent_events: list[dict[str, Any]],
        *,
        signaling_token_hash: str | None = None,
    ) -> Session:
        self.calls.append(
            {
                "user_id": user_id,
                "screen_context": screen_context,
                "recent_events": recent_events,
                "signaling_token_hash": signaling_token_hash,
            }
        )
        return Session(
            session_id=_SESSION_ID,
            user_id=user_id,
            state=SessionState.CREATED,
            started_at=_STARTED_AT,
            ended_at=None,
        )


def _conversation(
    *,
    session_id: str = _SESSION_ID,
    user_id: str = _MERCHANT_ID,
    state: str = SessionState.IN_CALL.value,
    ended_at: datetime | None = None,
) -> Conversation:
    return Conversation(
        session_id=session_id,
        user_id=user_id,
        state=state,
        signaling_token_hash="0" * 64,
        started_at=_STARTED_AT,
        ended_at=ended_at,
    )


def _conversation_repo(
    *,
    conversation: Conversation | None = None,
    turns: list[ConversationTurn] | None = None,
) -> type:
    class _FakeConversationRepo:
        def __init__(self, db: Any) -> None:
            self.db = db

        async def get(self, session_id: str) -> Conversation | None:
            return conversation

        async def list_turns(self, session_id: str) -> list[ConversationTurn]:
            return turns if turns is not None else []

    return _FakeConversationRepo


def _tool_audit_repo(invocations: list[ToolInvocation] | None = None) -> type:
    class _FakeToolAuditRepo:
        def __init__(self, db: Any) -> None:
            self.db = db

        async def list_for_session(self, session_id: str) -> list[ToolInvocation]:
            return invocations if invocations is not None else []

    return _FakeToolAuditRepo


def _turn(turn_no: int) -> ConversationTurn:
    return ConversationTurn(
        session_id=_SESSION_ID,
        turn_no=turn_no,
        role="user" if turn_no % 2 else "agent",
        started_at=_STARTED_AT,
    )


def _invocation(
    tool_name: str, *, status: str = "ok", output: dict[str, Any] | None = None
) -> ToolInvocation:
    return ToolInvocation(
        session_id=_SESSION_ID,
        tool_name=tool_name,
        input={},
        output=output,
        status=status,
        latency_ms=12,
    )


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def session_manager() -> _FakeSessionManager:
    return _FakeSessionManager()


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: FakeRedis,
    session_manager: _FakeSessionManager,
) -> Iterator[TestClient]:
    """A fresh `create_app()` per test. The Phase-3 voice env is set here
    (never a real `.env`) so `app.state.settings` carries a usable
    TURN/signaling block — `Settings`' voice fields are
    optional-with-default precisely so agent-api boots without them, which
    makes these four routes the surface that actually needs them."""
    monkeypatch.setenv("JWT_SECRET", _JWT_SECRET)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.setenv("TURN_SECRET", _TURN_SECRET)
    monkeypatch.setenv("COTURN_HOST", _COTURN_HOST)
    monkeypatch.setenv("SIGNALING_PUBLIC_URL", _SIGNALING_URL)
    monkeypatch.setenv("SIGNALING_TOKEN_TTL_S", str(_SIGNALING_TTL_S))
    get_settings.cache_clear()

    from app.api.main import create_app

    fastapi_app = create_app()

    async def _override_get_db() -> AsyncIterator[_SpySession]:
        yield _SpySession()

    async def _override_rate_limit() -> None:
        return None

    redis_client = RedisClient(fake_redis, session_ttl_seconds=86_400)  # type: ignore[arg-type]

    fastapi_app.dependency_overrides[get_db] = _override_get_db
    fastapi_app.dependency_overrides[require_rate_limit] = _override_rate_limit
    fastapi_app.dependency_overrides[get_redis] = lambda: redis_client
    fastapi_app.dependency_overrides[get_session_manager] = lambda: session_manager

    with TestClient(fastapi_app) as test_client:
        yield test_client

    get_settings.cache_clear()


def _create_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "user_id": _MERCHANT_ID,
        "screen_context": None,
        "recent_events": [],
    }
    body.update(overrides)
    return body


# --------------------------------------------------------------------------
# POST /v1/sessions (docs/13 §2.1)
# --------------------------------------------------------------------------


def test_create_session_returns_the_connect_bundle(client: TestClient) -> None:
    resp = client.post("/v1/sessions", headers=_AUTH_HEADERS, json=_create_body())

    body = resp.json()
    assert resp.status_code == 201
    assert body["success"] is True
    assert body["error"] is None
    assert body["meta"] is None
    data = body["data"]
    assert data["session_id"] == _SESSION_ID
    assert data["signaling_url"] == _SIGNALING_URL
    assert data["signaling_token"].startswith("st_")
    assert datetime.fromisoformat(data["expires"]) > datetime.now(UTC)


def test_create_session_response_validates_against_the_protocol_schema(
    client: TestClient,
) -> None:
    """docs/13 §8: `protocol/` is the contract authority, not the
    generated OpenAPI. What the route actually emits must satisfy the same
    schema the frozen fixture does."""
    resp = client.post("/v1/sessions", headers=_AUTH_HEADERS, json=_create_body())

    _assert_valid(resp.json(), _load_schema("session_create_response.v1"))


def test_create_session_ice_servers_are_stun_then_turn(client: TestClient) -> None:
    """docs/13 §2.1's exact shape: STUN first with no credential keys at
    all, then the TURN entry with both URLs and the HMAC pair."""
    resp = client.post("/v1/sessions", headers=_AUTH_HEADERS, json=_create_body())

    stun, turn = resp.json()["data"]["ice_servers"]
    assert stun == {"urls": [f"stun:{_COTURN_HOST}:3478"]}
    assert turn["urls"] == [
        f"turn:{_COTURN_HOST}:3478?transport=udp",
        f"turns:{_COTURN_HOST}:5349",
    ]
    assert turn["username"].endswith(f":{_SESSION_ID}")
    assert turn["credential"]


def test_create_session_stores_only_the_token_digest_with_the_connect_ttl(
    client: TestClient, fake_redis: FakeRedis
) -> None:
    """The security core of this endpoint: Redis holds the SHA-256 of the
    token under `signal_token:{id}` with the 5-minute connect TTL, and
    never the plaintext (docs/13 §6.2)."""
    resp = client.post("/v1/sessions", headers=_AUTH_HEADERS, json=_create_body())

    token = resp.json()["data"]["signaling_token"]
    key = f"signal_token:{_SESSION_ID}"
    assert fake_redis.strings[key] == hash_signaling_token(token)
    assert fake_redis.ttls[key] == _SIGNALING_TTL_S
    assert token not in fake_redis.strings.values()


def test_create_session_passes_the_real_digest_to_the_session_manager(
    client: TestClient, session_manager: _FakeSessionManager
) -> None:
    """Batch-D change: `SessionManager.create()` now persists the digest
    of a REAL token instead of minting a placeholder — the DB column and
    the Redis key must carry the same value."""
    resp = client.post("/v1/sessions", headers=_AUTH_HEADERS, json=_create_body())

    token = resp.json()["data"]["signaling_token"]
    assert session_manager.calls[0]["signaling_token_hash"] == hash_signaling_token(token)


def test_create_session_rejects_a_user_id_that_is_not_the_jwt_sub(
    client: TestClient, session_manager: _FakeSessionManager
) -> None:
    """docs/13 §1.2: verified, not honored. The session must never be
    created at all — asserting the 400 alone would pass even if the route
    minted for the wrong merchant first and complained afterwards."""
    resp = client.post(
        "/v1/sessions", headers=_AUTH_HEADERS, json=_create_body(user_id=_OTHER_MERCHANT_ID)
    )

    body = resp.json()
    assert resp.status_code == 400
    assert body["error"]["code"] == "VALIDATION_SCHEMA"
    assert body["error"]["details"]["fields"][0]["loc"] == ["body", "user_id"]
    assert session_manager.calls == []


def test_create_session_never_echoes_the_submitted_user_id(client: TestClient) -> None:
    """Neither the attacker's value nor the server's `sub` belongs in the
    rejection body."""
    resp = client.post(
        "/v1/sessions", headers=_AUTH_HEADERS, json=_create_body(user_id=_OTHER_MERCHANT_ID)
    )

    assert _OTHER_MERCHANT_ID not in resp.text
    assert _MERCHANT_ID not in resp.text


def test_create_session_uses_the_principal_not_the_body_user_id(
    client: TestClient, session_manager: _FakeSessionManager
) -> None:
    client.post("/v1/sessions", headers=_AUTH_HEADERS, json=_create_body())

    assert session_manager.calls[0]["user_id"] == _MERCHANT_ID


def test_create_session_accepts_the_supported_screen_context_version(
    client: TestClient, session_manager: _FakeSessionManager
) -> None:
    resp = client.post(
        "/v1/sessions",
        headers=_AUTH_HEADERS,
        json=_create_body(screen_context={"v": "screen_context/v1", "screen": "PaymentScreen"}),
    )

    assert resp.status_code == 201
    assert session_manager.calls[0]["screen_context"] == {
        "v": "screen_context/v1",
        "screen": "PaymentScreen",
    }


# --------------------------------------------------------------------------
# Phase-4 T3: SnapshotIngestor wiring at session-create (docs/08 §3.1/§4.1)
#
# docs/08 §7's closing line — "the pipeline degrades context, never
# conversation" — is the behavior under test in the four cases below: a
# session-create request must succeed (201) regardless of whether its
# screen_context is missing, schema-invalid, an unsupported version, or
# oversized. Only a genuinely SCHEMA-VALID snapshot actually lands in
# `ctx:{session_id}`; every degraded case leaves that key absent (or,
# same net effect, does not exist yet), identical to a client that sent
# `screen_context: null`.
# --------------------------------------------------------------------------


def test_create_session_persists_a_valid_screen_context_snapshot(
    client: TestClient,
) -> None:
    """The real `SnapshotIngestor` wiring: a fully schema-valid IR is
    written to `ctx:{session_id}` at the REST entrypoint's genesis
    `seq=0` (docs/08 §4.1 judgment call 4)."""
    screen_context = load_fixture("screen_context_payment_decline")

    resp = client.post(
        "/v1/sessions", headers=_AUTH_HEADERS, json=_create_body(screen_context=screen_context)
    )

    assert resp.status_code == 201


def test_create_session_stores_the_ingested_snapshot_in_redis(
    client: TestClient, fake_redis: FakeRedis
) -> None:
    screen_context = load_fixture("screen_context_payment_decline")

    client.post(
        "/v1/sessions", headers=_AUTH_HEADERS, json=_create_body(screen_context=screen_context)
    )

    stored = json.loads(fake_redis.strings[f"ctx:{_SESSION_ID}"])
    assert stored["screen"] == "PaymentScreen"
    assert stored["seq"] == 0
    assert "received_ts" in stored


def test_create_session_rejects_an_unknown_screen_context_version_but_still_creates_the_session(
    client: TestClient, session_manager: _FakeSessionManager, fake_redis: FakeRedis
) -> None:
    """An unsupported `v` used to fail the whole `POST /v1/sessions` call
    (`VALIDATION_UNSUPPORTED_VERSION`, pre-Phase-4-T3). Now it degrades
    only the screen-context slot: `SnapshotIngestor` rejects it
    (`REJECTED_ENVELOPE`), nothing is written to `ctx:{session_id}`, but
    the session itself is created exactly as if `screen_context` had been
    `null` — the call can still start."""
    resp = client.post(
        "/v1/sessions",
        headers=_AUTH_HEADERS,
        json=_create_body(screen_context={"v": "screen_context/v9", "screen": "PaymentScreen"}),
    )

    assert resp.status_code == 201
    assert session_manager.calls[0]["user_id"] == _MERCHANT_ID
    assert f"ctx:{_SESSION_ID}" not in fake_redis.strings


def test_create_session_survives_a_redis_failure_while_persisting_screen_context(
    client: TestClient,
    session_manager: _FakeSessionManager,
    fake_redis: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review fix (CRITICAL, Phase-4 T3 review): `SnapshotIngestor
    .ingest_initial_snapshot`'s final step (`_write_ctx`) is an unguarded
    Redis `SET` — before the fix, a connection drop/timeout there
    propagated uncaught through this route into `ErrorEnvelopeMiddleware`,
    turning an already-successful session creation (the `conversations`
    row is already committed by this point) into a client-visible 500. A
    Redis failure while persisting screen-context must degrade the slot,
    never the request — the session is created exactly as if
    `screen_context` had failed validation (docs/08 §7's "degrades
    context, never conversation").

    The failure is injected only for the `ctx:{session_id}` key — a
    blanket failure on every `SET` would also break the route's separate,
    pre-existing, equally-unguarded signaling-token write
    (`store_signaling_token`, `_issue_connect_bundle`), which is a real
    but different, already-accepted risk this test is not about."""
    real_set = fake_redis.set

    async def _fail_only_ctx_key(key: str, value: str, ex: int | None = None) -> None:
        if key.startswith("ctx:"):
            raise RedisError("redis down")
        await real_set(key, value, ex=ex)

    monkeypatch.setattr(fake_redis, "set", _fail_only_ctx_key)

    resp = client.post(
        "/v1/sessions",
        headers=_AUTH_HEADERS,
        json=_create_body(screen_context=load_fixture("screen_context_payment_decline")),
    )

    assert resp.status_code == 201
    assert session_manager.calls[0]["user_id"] == _MERCHANT_ID
    assert f"ctx:{_SESSION_ID}" not in fake_redis.strings


def test_create_session_accepts_a_schema_invalid_screen_context_without_failing(
    client: TestClient, fake_redis: FakeRedis
) -> None:
    """A supported version marker with a field that fails the full
    `screen_context.v1.json` schema (`screen` must be a string) is
    `REJECTED_SCHEMA`, not a request failure — the missing-required-field
    case `_require_supported_screen_context` never used to check at all,
    since that placeholder only ever looked at the version marker."""
    resp = client.post(
        "/v1/sessions",
        headers=_AUTH_HEADERS,
        json=_create_body(
            screen_context={
                "v": "screen_context/v1",
                "screen": 123,
                "flow": "",
                "components": [],
                "last_action": None,
                "last_api": None,
                "dirty_fields": [],
                "loading": False,
            }
        ),
    )

    assert resp.status_code == 201
    assert f"ctx:{_SESSION_ID}" not in fake_redis.strings


def test_create_session_accepts_and_recompresses_an_oversized_screen_context(
    client: TestClient, fake_redis: FakeRedis
) -> None:
    """A screen_context whose serialized size exceeds the 8 KiB check-(b)
    cap is routed through `ContextCompressor`'s docs/07 §7 drop ladder
    (`SnapshotIngestor._enforce_size_cap_snapshot`) rather than rejected
    outright — the ladder's rung 5 drops every non-minimal-role component
    (`image` is not one of the six survivors), so this specific oversize
    shape always converges on a schema-valid, well-under-budget IR and
    the session-create call succeeds with the recompressed snapshot
    actually stored."""
    oversized = {
        "v": "screen_context/v1",
        "screen": "DashboardScreen",
        "flow": "",
        "components": [
            {"role": "image", "label": f"icon-{n}-" + "x" * 100} for n in range(150)
        ],
        "last_action": None,
        "last_api": None,
        "dirty_fields": [],
        "loading": False,
    }
    assert len(json.dumps(oversized)) > 8 * 1024  # the fixture really is oversized

    resp = client.post(
        "/v1/sessions", headers=_AUTH_HEADERS, json=_create_body(screen_context=oversized)
    )

    assert resp.status_code == 201
    stored = json.loads(fake_redis.strings[f"ctx:{_SESSION_ID}"])
    assert stored["components"] == []  # rung 5 dropped every `image` component


def test_create_session_survives_an_oversized_and_structurally_malformed_screen_context(
    client: TestClient, fake_redis: FakeRedis
) -> None:
    """Audit fix (2026-08-04): the `_ingest_initial_screen_context` guard
    used to be `except RedisError`, which covered only the `_write_ctx`
    failure it was written for. `ingest_initial_snapshot` runs its 8 KiB
    size cap BEFORE schema validation, and the oversize branch feeds the
    payload to `ContextCompressor`'s drop ladder, whose rungs call
    `component.get("role")` on every entry of `components`. A payload that
    is both over the cap AND structurally malformed — `components` holding
    strings instead of objects, i.e. a buggy or outdated client build —
    therefore raised `AttributeError` straight past the guard and turned
    an already-committed session creation into a client-visible 500.

    That is the exact failure mode the original CRITICAL fix exists to
    prevent (`test_create_session_survives_a_redis_failure_...` above),
    reached through a different door: docs/08 §7's rule is absolute — the
    pipeline degrades context, never conversation. This payload must yield
    a normal 201 with an empty context slot, identical to sending
    `screen_context: null`.

    Ordering matters to this test: the malformed `components` must ALSO
    push the payload over the cap, since a malformed-but-small payload
    never reaches the ladder at all (schema validation rejects it first,
    the already-covered `REJECTED_SCHEMA` path)."""
    malformed_and_oversized = {
        "v": "screen_context/v1",
        "screen": "PaymentScreen",
        "flow": "vendor_payment",
        # Strings, not objects -- the ladder calls .get("role") on each.
        "components": ["not-an-object-" + "x" * 40 for _ in range(300)],
        "last_action": None,
        "last_api": None,
        "dirty_fields": [],
        "loading": False,
    }
    assert len(json.dumps(malformed_and_oversized)) > 8 * 1024  # really is oversized

    resp = client.post(
        "/v1/sessions",
        headers=_AUTH_HEADERS,
        json=_create_body(screen_context=malformed_and_oversized),
    )

    assert resp.status_code == 201
    assert f"ctx:{_SESSION_ID}" not in fake_redis.strings


def test_create_session_rejects_unknown_body_fields(client: TestClient) -> None:
    """`additionalProperties: false` in
    protocol/schemas/session_create_request.v1.json — new request fields
    are a server-contract change, never a client improvisation."""
    resp = client.post(
        "/v1/sessions", headers=_AUTH_HEADERS, json=_create_body(surprise="hello")
    )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_SCHEMA"


def test_create_session_rejects_a_malformed_recent_event(client: TestClient) -> None:
    resp = client.post(
        "/v1/sessions",
        headers=_AUTH_HEADERS,
        json=_create_body(recent_events=[{"type": "tap"}]),
    )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_SCHEMA"


def test_create_session_rejects_an_oversized_recent_events_array(client: TestClient) -> None:
    """Defensive cap (`_MAX_RECENT_EVENTS`): docs/13 §2.1 sends "the last
    ~15" events and the schema sets no `maxItems`, so an unbounded array
    would be a free parse amplifier. One over the cap must be rejected
    before anything is minted."""
    events = [
        {"type": "tap", "name": f"e{n}", "ts": 1_784_536_440_000 + n}
        for n in range(_MAX_RECENT_EVENTS + 1)
    ]

    resp = client.post(
        "/v1/sessions", headers=_AUTH_HEADERS, json=_create_body(recent_events=events)
    )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_SCHEMA"


def test_create_session_accepts_a_recent_events_array_at_the_cap(
    client: TestClient, session_manager: _FakeSessionManager
) -> None:
    events = [
        {"type": "tap", "name": f"e{n}", "ts": 1_784_536_440_000 + n}
        for n in range(_MAX_RECENT_EVENTS)
    ]

    resp = client.post(
        "/v1/sessions", headers=_AUTH_HEADERS, json=_create_body(recent_events=events)
    )

    assert resp.status_code == 201
    assert len(session_manager.calls[0]["recent_events"]) == _MAX_RECENT_EVENTS


# --------------------------------------------------------------------------
# recent_events -> EventLog (docs/08 §4.2, §6; docs/13 §2.1's "write
# ctx:a1f3c9 AND the events list")
#
# Until this landed the route accepted `recent_events`, handed them to
# `SessionManager.create()` (which "accepts and ignores" them) and never
# called `EventLog.append` — so `ContextBuilder`'s slot-5 timeline read
# back empty for the opening turns of every call. The three properties
# below are what "the timeline actually reaches the agent" means:
# per-variant fields survive, order survives, and a persistence failure
# still degrades context rather than the call.
# --------------------------------------------------------------------------


def _stored_events(fake_redis: FakeRedis) -> list[dict[str, Any]]:
    """What `EventLog.get_events` / `ContextBuilder` will read back —
    `ctx:{id}:events` in RPUSH order, i.e. oldest first."""
    return [json.loads(entry) for entry in fake_redis.lists.get(f"ctx:{_SESSION_ID}:events", [])]


def test_create_session_persists_recent_events_to_the_event_log(
    client: TestClient, fake_redis: FakeRedis
) -> None:
    events = [
        {
            "type": "nav",
            "name": "PaymentScreen",
            "from": "DashboardScreen",
            "ts": 1_784_536_395_000,
        },
        {"type": "tap", "name": "Pay Now", "screen": "PaymentScreen", "ts": 1_784_536_440_000},
    ]

    resp = client.post(
        "/v1/sessions", headers=_AUTH_HEADERS, json=_create_body(recent_events=events)
    )

    assert resp.status_code == 201
    assert _stored_events(fake_redis) == events


def test_create_session_persists_every_per_variant_event_field(
    client: TestClient, fake_redis: FakeRedis
) -> None:
    """The whole point of the round trip: `protocol/schemas/app_event.v1
    .json`'s per-type required fields must survive from the wire into
    `ctx:{id}:events`, because that is what `ContextCompressor
    ._render_event_line` renders from. `api_error`'s `status`/`code` is the
    single highest-value diagnostic in the pre-call timeline — an agent that
    loses it opens the call blind to the 402 the merchant just hit — and
    `dialog.visible` is what distinguishes a dialog the merchant is staring
    at from one they already dismissed.

    The payload is `protocol/fixtures/session_create_request.json`'s own
    `recent_events` array, so this test fails the moment the route stops
    carrying what the frozen fixture ships.
    """
    events = _load_fixture("session_create_request")["recent_events"]
    assert {event["type"] for event in events} == {"nav", "input", "tap", "api_error", "dialog"}

    resp = client.post(
        "/v1/sessions", headers=_AUTH_HEADERS, json=_create_body(recent_events=events)
    )

    assert resp.status_code == 201
    stored = _stored_events(fake_redis)
    assert stored == events  # every per-variant key, verbatim

    api_error = next(event for event in stored if event["type"] == "api_error")
    assert api_error["status"] == 402
    assert api_error["code"] == "DAILY_LIMIT_EXCEEDED"
    assert next(event for event in stored if event["type"] == "dialog")["visible"] is True
    assert next(event for event in stored if event["type"] == "nav")["from"] == "DashboardScreen"


def test_create_session_stores_recent_events_oldest_first(
    client: TestClient, fake_redis: FakeRedis
) -> None:
    """`session_create_request.v1.json` pins `recent_events` oldest-first
    and `EventLog.append` is RPUSH, so list position *is* chronological
    order — the invariant `ContextCompressor.render_timeline_slot` relies on
    when it slices `events[-15:]` for "the newest 15". Fed a ts-ascending
    array, the stored list must come back ts-ascending too: nothing in the
    route reverses, re-sorts, or otherwise second-guesses the client.
    """
    events = [
        {"type": "tap", "name": f"e{n}", "ts": 1_784_536_440_000 + n * 1_000} for n in range(5)
    ]

    client.post("/v1/sessions", headers=_AUTH_HEADERS, json=_create_body(recent_events=events))

    stored = _stored_events(fake_redis)
    assert [event["name"] for event in stored] == ["e0", "e1", "e2", "e3", "e4"]
    assert [event["ts"] for event in stored] == sorted(event["ts"] for event in stored)


def test_create_session_survives_a_failure_while_persisting_recent_events(
    client: TestClient,
    session_manager: _FakeSessionManager,
    fake_redis: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same absolute rule `_ingest_initial_screen_context` is held to
    (docs/08 §7: "the pipeline degrades context, never conversation"),
    applied to the timeline write. `_persist_recent_events` runs after
    `session_manager.create()` has already committed the `conversations`
    row, so a Redis failure there must never turn an already-successful
    session creation into a client-visible 500 — the merchant gets their
    call with an empty slot-5 timeline, exactly as if they had sent
    `recent_events: []`.

    Audit fix (2026-08-05): `EventLog.append_many` now pipelines RPUSH/
    LTRIM/EXPIRE into one round trip rather than calling the top-level
    async `rpush` once per event, so the failure must be injected into
    `FakePipeline.execute`'s dispatch target -- the private, synchronous
    `_rpush` -- not the public async method the pipeline path no longer
    calls.

    Correcting this docstring's own first version, which claimed patching
    the old `rpush` target would leave this test "silently passing for the
    wrong reason": it would not. Reproduced 2026-08-05 -- the injection
    goes inert, the write really lands, and the key-absence assertion
    below fails loudly. The old target is still wrong, for a different
    reason: the `except Exception` guard this test exists to cover never
    runs, so neither a red nor a green result here would say anything
    about it.
    """

    def _fail(key: str, *values: str) -> None:
        raise RedisError("redis down")

    monkeypatch.setattr(fake_redis, "_rpush", _fail)

    resp = client.post(
        "/v1/sessions",
        headers=_AUTH_HEADERS,
        json=_create_body(
            recent_events=[{"type": "tap", "name": "Pay Now", "ts": 1_784_536_440_000}]
        ),
    )

    assert resp.status_code == 201
    assert session_manager.calls[0]["user_id"] == _MERCHANT_ID
    assert f"ctx:{_SESSION_ID}:events" not in fake_redis.lists


def test_create_session_with_no_recent_events_writes_no_event_key(
    client: TestClient, fake_redis: FakeRedis
) -> None:
    """The Phase-3 body (`recent_events: []`) must not create an empty
    `ctx:{id}:events` key — an absent key and an empty list read back
    identically through `EventLog.get_events`, and not writing keeps the
    no-context path allocation-free."""
    resp = client.post("/v1/sessions", headers=_AUTH_HEADERS, json=_create_body())

    assert resp.status_code == 201
    assert f"ctx:{_SESSION_ID}:events" not in fake_redis.lists


def test_create_session_requires_auth(client: TestClient) -> None:
    resp = client.post("/v1/sessions", json=_create_body())

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTH_MISSING_TOKEN"


def test_create_session_without_turn_config_is_500_and_stores_no_token(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: FakeRedis,
    session_manager: _FakeSessionManager,
) -> None:
    """An agent-api booted with no `TURN_SECRET` can't mint a usable call
    for anyone — an operator error, rendered as the generic `500 INTERNAL`
    with no config detail in the body. The assertion that matters is the
    second one: every fallible computation runs BEFORE the Redis write, so
    a misconfigured deployment leaves no orphan token digests behind."""
    monkeypatch.setenv("JWT_SECRET", _JWT_SECRET)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.setenv("COTURN_HOST", _COTURN_HOST)
    monkeypatch.setenv("SIGNALING_PUBLIC_URL", _SIGNALING_URL)
    monkeypatch.delenv("TURN_SECRET", raising=False)
    get_settings.cache_clear()

    from app.api.main import create_app

    fastapi_app = create_app()

    async def _override_get_db() -> AsyncIterator[_SpySession]:
        yield _SpySession()

    async def _override_rate_limit() -> None:
        return None

    redis_client = RedisClient(fake_redis, session_ttl_seconds=86_400)  # type: ignore[arg-type]
    fastapi_app.dependency_overrides[get_db] = _override_get_db
    fastapi_app.dependency_overrides[require_rate_limit] = _override_rate_limit
    fastapi_app.dependency_overrides[get_redis] = lambda: redis_client
    fastapi_app.dependency_overrides[get_session_manager] = lambda: session_manager

    with TestClient(fastapi_app) as test_client:
        resp = test_client.post("/v1/sessions", headers=_AUTH_HEADERS, json=_create_body())

    get_settings.cache_clear()

    body = resp.json()
    assert resp.status_code == 500
    assert body["error"]["code"] == "INTERNAL"
    assert "TURN_SECRET" not in resp.text
    assert fake_redis.strings == {}


# --------------------------------------------------------------------------
# POST /v1/sessions/{id}/token (docs/13 §2.2 reconnect)
# --------------------------------------------------------------------------


def test_remint_returns_a_fresh_token_and_replaces_the_stored_digest(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fake_redis: FakeRedis
) -> None:
    monkeypatch.setattr(
        "app.api.routes.sessions.ConversationRepo",
        _conversation_repo(conversation=_conversation()),
    )

    first = client.post("/v1/sessions", headers=_AUTH_HEADERS, json=_create_body())
    first_token = first.json()["data"]["signaling_token"]

    resp = client.post(f"/v1/sessions/{_SESSION_ID}/token", headers=_AUTH_HEADERS)

    body = resp.json()
    assert resp.status_code == 200
    new_token = body["data"]["signaling_token"]
    assert new_token != first_token
    assert body["data"]["session_id"] == _SESSION_ID
    # The replacement digest is stored, so the token it replaced is dead.
    assert fake_redis.strings[f"signal_token:{_SESSION_ID}"] == hash_signaling_token(new_token)


def test_remint_on_an_ended_session_is_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.api.routes.sessions.ConversationRepo",
        _conversation_repo(
            conversation=_conversation(state=SessionState.ENDED.value, ended_at=_ENDED_AT)
        ),
    )

    resp = client.post(f"/v1/sessions/{_SESSION_ID}/token", headers=_AUTH_HEADERS)

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "SESSION_ALREADY_ENDED"


def test_remint_on_another_merchants_session_is_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fake_redis: FakeRedis
) -> None:
    """Indistinguishable from a wrong id (docs/13 §1.1) — and no
    credential is minted for it."""
    monkeypatch.setattr(
        "app.api.routes.sessions.ConversationRepo",
        _conversation_repo(conversation=_conversation(user_id=_OTHER_MERCHANT_ID)),
    )

    resp = client.post(f"/v1/sessions/{_SESSION_ID}/token", headers=_AUTH_HEADERS)

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "SESSION_NOT_FOUND"
    assert fake_redis.strings == {}


def test_remint_on_an_unknown_session_is_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.api.routes.sessions.ConversationRepo", _conversation_repo(conversation=None)
    )

    resp = client.post("/v1/sessions/does-not-exist/token", headers=_AUTH_HEADERS)

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "SESSION_NOT_FOUND"


# --------------------------------------------------------------------------
# DELETE /v1/sessions/{id} (docs/13 §2.2)
# --------------------------------------------------------------------------


def test_delete_session_publishes_end_and_returns_the_terminal_state(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fake_redis: FakeRedis
) -> None:
    monkeypatch.setattr(
        "app.api.routes.sessions.ConversationRepo",
        _conversation_repo(conversation=_conversation()),
    )

    resp = client.delete(f"/v1/sessions/{_SESSION_ID}", headers=_AUTH_HEADERS)

    assert resp.status_code == 200
    assert resp.json()["data"] == {"session_id": _SESSION_ID, "state": "ended"}
    assert fake_redis.published == [(f"session_control:{_SESSION_ID}", SESSION_CONTROL_END)]


def test_delete_session_is_idempotent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fake_redis: FakeRedis
) -> None:
    """docs/13 §2.2: "a repeat call returns the same body, not a 409" —
    hang-up races the in-band `bye` and aiortc teardown constantly. The
    second call is asserted against an ALREADY-ENDED row, which is the
    state the first call's publish leads to."""
    monkeypatch.setattr(
        "app.api.routes.sessions.ConversationRepo",
        _conversation_repo(conversation=_conversation()),
    )
    first = client.delete(f"/v1/sessions/{_SESSION_ID}", headers=_AUTH_HEADERS)

    monkeypatch.setattr(
        "app.api.routes.sessions.ConversationRepo",
        _conversation_repo(
            conversation=_conversation(state=SessionState.ENDED.value, ended_at=_ENDED_AT)
        ),
    )
    second = client.delete(f"/v1/sessions/{_SESSION_ID}", headers=_AUTH_HEADERS)

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert len(fake_redis.published) == 2


def test_delete_on_another_merchants_session_is_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fake_redis: FakeRedis
) -> None:
    monkeypatch.setattr(
        "app.api.routes.sessions.ConversationRepo",
        _conversation_repo(conversation=_conversation(user_id=_OTHER_MERCHANT_ID)),
    )

    resp = client.delete(f"/v1/sessions/{_SESSION_ID}", headers=_AUTH_HEADERS)

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "SESSION_NOT_FOUND"
    assert fake_redis.published == []


# --------------------------------------------------------------------------
# GET /v1/sessions/{id}/summary (docs/13 §2.3)
# --------------------------------------------------------------------------


def test_summary_before_the_post_call_pipeline_lands_is_404_with_retry_after(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.api.routes.sessions.ConversationRepo",
        _conversation_repo(conversation=_conversation(state=SessionState.IN_CALL.value)),
    )

    resp = client.get(f"/v1/sessions/{_SESSION_ID}/summary", headers=_AUTH_HEADERS)

    body = resp.json()
    assert resp.status_code == 404
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"]["code"] == "SESSION_SUMMARY_PENDING"
    assert resp.headers["Retry-After"] == "2"


def test_summary_after_the_drain_is_assembled_mechanically(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """docs/13 §2.3's field set, built from `conversations` +
    `conversation_turns` + `tool_invocations` with no LLM in the loop."""
    monkeypatch.setattr(
        "app.api.routes.sessions.ConversationRepo",
        _conversation_repo(
            conversation=_conversation(state=SessionState.ENDED.value, ended_at=_ENDED_AT),
            turns=[_turn(n) for n in range(1, 16)],
        ),
    )
    monkeypatch.setattr(
        "app.api.routes.sessions.ToolAuditRepo",
        _tool_audit_repo(
            [
                _invocation("get_payment_status"),
                _invocation("get_wallet_balance"),
                _invocation(
                    "request_limit_increase",
                    output={
                        "request_id": "LMT-2026-0724-0913",
                        "status": "submitted",
                        "eta_hours": 4,
                    },
                ),
            ]
        ),
    )

    resp = client.get(f"/v1/sessions/{_SESSION_ID}/summary", headers=_AUTH_HEADERS)

    data = resp.json()["data"]
    assert resp.status_code == 200
    assert data["session_id"] == _SESSION_ID
    assert data["started_at"] == _STARTED_AT.isoformat()
    assert data["duration_s"] == 316
    assert data["turn_count"] == 15
    assert data["actions"] == [
        {"tool": "get_payment_status", "status": "ok"},
        {"tool": "get_wallet_balance", "status": "ok"},
        {"tool": "request_limit_increase", "status": "ok"},
    ]
    assert data["resolution"] == {
        "type": "limit_increase_requested",
        "reference": "LMT-2026-0724-0913",
        "eta_hours": 4,
    }
    assert "LMT-2026-0724-0913" in data["summary"]
    assert "15 turns" in data["summary"]
    # docs/13 §2.3: cost fields and the transcript are deliberately absent.
    assert "cost" not in data
    assert "transcript" not in data


def test_summary_without_a_limit_increase_has_a_null_resolution(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.api.routes.sessions.ConversationRepo",
        _conversation_repo(
            conversation=_conversation(state=SessionState.ENDED.value, ended_at=_ENDED_AT),
            turns=[_turn(1)],
        ),
    )
    monkeypatch.setattr(
        "app.api.routes.sessions.ToolAuditRepo", _tool_audit_repo([_invocation("get_orders")])
    )

    resp = client.get(f"/v1/sessions/{_SESSION_ID}/summary", headers=_AUTH_HEADERS)

    data = resp.json()["data"]
    assert data["resolution"] is None
    assert data["actions"] == [{"tool": "get_orders", "status": "ok"}]


def test_summary_ignores_a_failed_limit_increase(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`resolution` is pulled from a SUCCESSFUL invocation only — a denied
    or errored attempt is still listed under `actions`, but the call
    reached no resolution."""
    monkeypatch.setattr(
        "app.api.routes.sessions.ConversationRepo",
        _conversation_repo(
            conversation=_conversation(state=SessionState.ENDED.value, ended_at=_ENDED_AT),
            turns=[_turn(1)],
        ),
    )
    monkeypatch.setattr(
        "app.api.routes.sessions.ToolAuditRepo",
        _tool_audit_repo(
            [
                _invocation(
                    "request_limit_increase",
                    status="error",
                    output={"request_id": "LMT-2026-0724-0913"},
                )
            ]
        ),
    )

    resp = client.get(f"/v1/sessions/{_SESSION_ID}/summary", headers=_AUTH_HEADERS)

    data = resp.json()["data"]
    assert data["resolution"] is None
    assert data["actions"] == [{"tool": "request_limit_increase", "status": "error"}]


def test_summary_on_another_merchants_session_is_404_not_pending(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ownership is checked before the pending gate, so a stranger can't
    even learn whether someone else's call has finished."""
    monkeypatch.setattr(
        "app.api.routes.sessions.ConversationRepo",
        _conversation_repo(
            conversation=_conversation(
                user_id=_OTHER_MERCHANT_ID, state=SessionState.ENDED.value, ended_at=_ENDED_AT
            )
        ),
    )

    resp = client.get(f"/v1/sessions/{_SESSION_ID}/summary", headers=_AUTH_HEADERS)

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "SESSION_NOT_FOUND"
    assert "Retry-After" not in resp.headers
