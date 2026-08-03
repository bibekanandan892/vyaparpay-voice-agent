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

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient

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
from tests.contract.test_voice_protocol import _assert_valid, _load_schema
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


def test_create_session_rejects_an_unknown_screen_context_version(
    client: TestClient, session_manager: _FakeSessionManager
) -> None:
    resp = client.post(
        "/v1/sessions",
        headers=_AUTH_HEADERS,
        json=_create_body(screen_context={"v": "screen_context/v9", "screen": "PaymentScreen"}),
    )

    body = resp.json()
    assert resp.status_code == 400
    assert body["error"]["code"] == "VALIDATION_UNSUPPORTED_VERSION"
    assert session_manager.calls == []


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
