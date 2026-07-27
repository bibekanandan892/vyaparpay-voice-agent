"""Route-level tests for app/api/routes/*.py — the full ASGI stack (real
`create_app()`, all four middleware layers, real routing) driven via
`TestClient`, but with no real Postgres/Redis: `get_db` and
`require_rate_limit` are swapped via `app.dependency_overrides` (per this
task's brief), and each route's repository class is monkeypatched at its
point of use (`app.api.routes.<module>.<Repo>`, the standard "patch where
it's looked up" rule) with a small fake that returns real domain
dataclasses/ORM instances — `PaymentStatus`, `DailyLimitCheck`,
`WalletAccount`, `Transaction`, ... — or raises a real `AppError`
subclass, whichever the test needs. No SQL, no network, fully hermetic.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_db, require_rate_limit
from app.api.errors import LimitRequestAlreadyPendingError, RateLimitedError
from app.config import get_settings
from app.data.repositories import (
    DailyLimitCheck,
    LimitIncreaseRequest,
    PaymentStatus,
    WalletDebitResult,
)
from app.models import Transaction, WalletAccount

_JWT_SECRET = "test-jwt-secret"
_MERCHANT_ID = "usr_rajesh01"


def _token(sub: str = _MERCHANT_ID, **claims: object) -> str:
    payload: dict[str, object] = {"sub": sub, "exp": datetime.now(UTC) + timedelta(hours=24)}
    payload.update(claims)
    return jwt.encode(payload, _JWT_SECRET, algorithm="HS256")


_AUTH_HEADERS = {"Authorization": f"Bearer {_token()}"}


class _SpySession:
    """Stands in for the request-scoped `AsyncSession` — records whether
    the route explicitly called `.commit()` (the declined-payment test
    checks this; `app/api/deps.py`'s real `get_db` is unit-tested
    separately in tests/api/test_deps.py, not re-verified here)."""

    def __init__(self) -> None:
        self.committed = False

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        pass


def _wallet_repo_returning(
    wallet: WalletAccount | None, *, debit_calls: list[tuple[str, int]] | None = None
) -> type:
    """`debit_if_sufficient`'s result is derived from `wallet.balance_paise`
    vs. the requested amount, matching the real repo's own logic closely
    enough for route-level testing — so the existing succeeded/declined-
    daily-limit tests (whose wallet fixtures carry ₹18,450, far above
    anything they attempt) need no changes to keep passing after this
    method was added (review fix — see module docstring history in
    payments.py). `debit_calls`, when passed, records every call so a
    test can assert the daily-limit-exceeded path never reaches this
    method at all (the route must short-circuit before touching the
    wallet once the limit check alone is enough to decline)."""

    class _FakeWalletRepo:
        def __init__(self, db: Any) -> None:
            self.db = db

        async def get_by_merchant(self, merchant_id: str) -> WalletAccount | None:
            return wallet

        async def debit_if_sufficient(
            self, merchant_id: str, amount_paise: int, *, with_for_update: bool = False
        ) -> WalletDebitResult | None:
            if debit_calls is not None:
                debit_calls.append((merchant_id, amount_paise))
            if wallet is None:
                return None
            if wallet.balance_paise < amount_paise:
                return WalletDebitResult(sufficient=False, balance_paise=wallet.balance_paise)
            return WalletDebitResult(sufficient=True, balance_paise=wallet.balance_paise)

    return _FakeWalletRepo


def _payment_repo(
    *,
    get_payment_status_result: PaymentStatus | None = None,
    check_daily_limit_result: DailyLimitCheck | None = None,
    record_daily_usage_calls: list[tuple[str, int]] | None = None,
) -> type:
    class _FakePaymentRepo:
        def __init__(self, db: Any) -> None:
            self.db = db

        async def get_payment_status(self, txn_id: str, merchant_id: str) -> PaymentStatus | None:
            return get_payment_status_result

        async def check_daily_limit(
            self,
            merchant_id: str,
            amount_paise: int,
            *,
            limit_type: str = "daily_txn",
            with_for_update: bool = False,
        ) -> DailyLimitCheck | None:
            return check_daily_limit_result

        async def record_daily_usage(
            self, merchant_id: str, amount_paise: int, *, limit_type: str = "daily_txn"
        ) -> None:
            if record_daily_usage_calls is not None:
                record_daily_usage_calls.append((merchant_id, amount_paise))

        async def create(
            self,
            *,
            txn_id: str,
            merchant_id: str,
            wallet_id: str,
            txn_type: str,
            amount_paise: int,
            counterparty: str,
            status: str,
            decline_code: str | None = None,
            http_status: int | None = None,
        ) -> Transaction:
            return Transaction(
                txn_id=txn_id,
                merchant_id=merchant_id,
                wallet_id=wallet_id,
                type=txn_type,
                amount_paise=amount_paise,
                counterparty=counterparty,
                status=status,
                decline_code=decline_code,
                http_status=http_status,
            )

    return _FakePaymentRepo


def _limit_repo(
    *, result: LimitIncreaseRequest | None = None, error: Exception | None = None
) -> type:
    class _FakeLimitRepo:
        def __init__(self, db: Any) -> None:
            self.db = db

        async def submit_increase_request(
            self,
            merchant_id: str,
            limit_type: str,
            current_limit_paise: int,
            requested_limit_paise: int,
        ) -> LimitIncreaseRequest:
            if error is not None:
                raise error
            assert result is not None
            return result

    return _FakeLimitRepo


@pytest.fixture
def spy_db() -> _SpySession:
    return _SpySession()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, spy_db: _SpySession) -> Iterator[TestClient]:
    """A fresh `create_app()` instance per test (not the `app.api.main`
    process singleton) so `dependency_overrides` from one test can never
    bleed into another. `get_db` yields the spy session; `require_rate_limit`
    is a no-op by default — the one test that needs it to actually fire
    builds its own app instance instead (see
    `test_create_payment_rate_limited_returns_429`)."""
    monkeypatch.setenv("JWT_SECRET", _JWT_SECRET)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    get_settings.cache_clear()

    from app.api.main import create_app

    fastapi_app = create_app()

    async def _override_get_db() -> AsyncIterator[_SpySession]:
        yield spy_db

    async def _override_rate_limit() -> None:
        return None

    fastapi_app.dependency_overrides[get_db] = _override_get_db
    fastapi_app.dependency_overrides[require_rate_limit] = _override_rate_limit

    with TestClient(fastapi_app) as test_client:
        yield test_client

    get_settings.cache_clear()


def _build_app_with_overrides(
    *, get_db_override: Any, rate_limit_override: Any
) -> FastAPI:
    from app.api.main import create_app

    fastapi_app = create_app()
    fastapi_app.dependency_overrides[get_db] = get_db_override
    fastapi_app.dependency_overrides[require_rate_limit] = rate_limit_override
    return fastapi_app


# --------------------------------------------------------------------------
# GET /v1/wallet (docs/13 §3.1)
# --------------------------------------------------------------------------


def test_get_wallet_returns_the_balance(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallet = WalletAccount(
        wallet_id="wal_rajesh01",
        merchant_id=_MERCHANT_ID,
        balance_paise=1_845_000,
        currency="INR",
        updated_at=datetime(2026, 7, 24, 14, 2, 11, tzinfo=UTC),
    )
    monkeypatch.setattr("app.api.routes.wallet.WalletRepo", _wallet_repo_returning(wallet))

    resp = client.get("/v1/wallet", headers=_AUTH_HEADERS)

    body = resp.json()
    assert resp.status_code == 200
    assert body["success"] is True
    assert body["data"]["wallet_id"] == "wal_rajesh01"
    assert body["data"]["balance_paise"] == 1_845_000
    assert body["data"]["currency"] == "INR"
    assert body["error"] is None


def test_get_wallet_missing_wallet_is_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.api.routes.wallet.WalletRepo", _wallet_repo_returning(None))

    resp = client.get("/v1/wallet", headers=_AUTH_HEADERS)

    body = resp.json()
    assert resp.status_code == 404
    assert body["success"] is False
    assert body["error"]["code"] == "RESOURCE_NOT_FOUND"


# --------------------------------------------------------------------------
# GET /v1/payments/{id}
# --------------------------------------------------------------------------


def test_get_payment_returns_status(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    status = PaymentStatus(
        payment_id="txn_0724_1414a",
        status="declined",
        amount_paise=24_500,
        counterparty="Amazon Business",
        decline_code="DAILY_LIMIT_EXCEEDED",
        limit_context=None,
    )
    monkeypatch.setattr(
        "app.api.routes.payments.PaymentRepo",
        _payment_repo(get_payment_status_result=status),
    )

    resp = client.get("/v1/payments/txn_0724_1414a", headers=_AUTH_HEADERS)

    body = resp.json()
    assert resp.status_code == 200
    assert body["data"]["payment_id"] == "txn_0724_1414a"
    assert body["data"]["decline_code"] == "DAILY_LIMIT_EXCEEDED"


def test_get_payment_unknown_id_is_404(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.api.routes.payments.PaymentRepo",
        _payment_repo(get_payment_status_result=None),
    )

    resp = client.get("/v1/payments/does-not-exist", headers=_AUTH_HEADERS)

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


# --------------------------------------------------------------------------
# POST /v1/payments (docs/13 §3.2 — the canonical 402)
# --------------------------------------------------------------------------


def _create_payment_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "type": "vendor_payment",
        "amount_paise": 24_500,
        "counterparty": "Amazon Business",
        "source": "wallet",
        "client_ref": "andr-1784536440-8821",
    }
    body.update(overrides)
    return body


def test_create_payment_succeeded_path_returns_201(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallet = WalletAccount(
        wallet_id="wal_rajesh01",
        merchant_id=_MERCHANT_ID,
        balance_paise=1_845_000,
        currency="INR",
        updated_at=datetime.now(UTC),
    )
    monkeypatch.setattr("app.api.routes.payments.WalletRepo", _wallet_repo_returning(wallet))
    monkeypatch.setattr(
        "app.api.routes.payments.PaymentRepo",
        _payment_repo(
            check_daily_limit_result=DailyLimitCheck(
                exceeded=False,
                limit_paise=2_500_000,
                used_paise=100_000,
                resets_at=datetime.now(UTC),
            )
        ),
    )

    resp = client.post(
        "/v1/payments", headers=_AUTH_HEADERS, json=_create_payment_body(amount_paise=5_000)
    )

    body = resp.json()
    assert resp.status_code == 201
    assert body["success"] is True
    assert body["data"]["status"] == "succeeded"
    assert body["data"]["amount_paise"] == 5_000
    assert body["data"]["counterparty"] == "Amazon Business"


def test_create_payment_succeeded_path_debits_wallet_and_records_daily_usage(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review finding (CRITICAL): the success path previously never
    touched wallet_accounts.balance_paise or merchant_limits.used_paise
    at all — a payment could be marked succeeded regardless of actual
    balance, and the daily-limit control went permanently inert after
    the seeded state. This asserts both writes actually happen, with
    the correct merchant/amount, exactly once, only on the success
    path."""
    wallet = WalletAccount(
        wallet_id="wal_rajesh01",
        merchant_id=_MERCHANT_ID,
        balance_paise=1_845_000,
        currency="INR",
        updated_at=datetime.now(UTC),
    )
    debit_calls: list[tuple[str, int]] = []
    usage_calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        "app.api.routes.payments.WalletRepo",
        _wallet_repo_returning(wallet, debit_calls=debit_calls),
    )
    monkeypatch.setattr(
        "app.api.routes.payments.PaymentRepo",
        _payment_repo(
            check_daily_limit_result=DailyLimitCheck(
                exceeded=False,
                limit_paise=2_500_000,
                used_paise=100_000,
                resets_at=datetime.now(UTC),
            ),
            record_daily_usage_calls=usage_calls,
        ),
    )

    resp = client.post(
        "/v1/payments", headers=_AUTH_HEADERS, json=_create_payment_body(amount_paise=5_000)
    )

    assert resp.status_code == 201
    assert debit_calls == [(_MERCHANT_ID, 5_000)]
    assert usage_calls == [(_MERCHANT_ID, 5_000)]


def test_create_payment_insufficient_balance_is_402_and_does_not_record_daily_usage(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, spy_db: _SpySession
) -> None:
    """Review finding (CRITICAL): `INSUFFICIENT_BALANCE` is a real,
    documented decline code that was never raised anywhere before this
    fix. Also asserts the ordering invariant that matters most: a
    payment declined for insufficient balance must NOT have charged the
    daily limit it never actually used (docs/12 §3.2's invariant is
    SUM(successful payouts) == used_paise, not "attempted")."""
    wallet = WalletAccount(
        wallet_id="wal_rajesh01",
        merchant_id=_MERCHANT_ID,
        balance_paise=1_000,  # less than the 5_000 paise attempted below
        currency="INR",
        updated_at=datetime.now(UTC),
    )
    usage_calls: list[tuple[str, int]] = []
    monkeypatch.setattr("app.api.routes.payments.WalletRepo", _wallet_repo_returning(wallet))
    monkeypatch.setattr(
        "app.api.routes.payments.PaymentRepo",
        _payment_repo(
            check_daily_limit_result=DailyLimitCheck(
                exceeded=False,
                limit_paise=2_500_000,
                used_paise=100_000,
                resets_at=datetime.now(UTC),
            ),
            record_daily_usage_calls=usage_calls,
        ),
    )

    resp = client.post(
        "/v1/payments", headers=_AUTH_HEADERS, json=_create_payment_body(amount_paise=5_000)
    )

    body = resp.json()
    assert resp.status_code == 402
    assert body["error"]["code"] == "INSUFFICIENT_BALANCE"
    assert body["error"]["details"]["attempted_paise"] == 5_000
    assert body["error"]["details"]["balance_paise"] == 1_000
    assert body["error"]["details"]["txn_id"].startswith("txn_")
    assert spy_db.committed is True  # the decline itself must still persist
    assert usage_calls == []  # never charged against a limit it never used


def test_create_payment_declined_matches_docs_worked_example(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, spy_db: _SpySession
) -> None:
    """docs/13 §3.2's exact 402 worked example: limit ₹25,000, ₹24,890
    used, ₹245 attempted -> `DAILY_LIMIT_EXCEEDED` with that precise
    message and `details` shape. Also asserts the declined transaction
    row was explicitly committed (`spy_db.committed`) despite the raised
    error — the one deviation from get_db's default rollback-on-exception
    this route makes on purpose (see payments.py's own comment) — and
    (review finding, ordering guard) that the wallet is never even
    touched once the daily-limit check alone is enough to decline: the
    route must short-circuit before `debit_if_sufficient`, not merely
    happen to produce the right error code despite reaching it.
    """
    wallet = WalletAccount(
        wallet_id="wal_rajesh01",
        merchant_id=_MERCHANT_ID,
        balance_paise=1_845_000,
        currency="INR",
        updated_at=datetime.now(UTC),
    )
    debit_calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        "app.api.routes.payments.WalletRepo",
        _wallet_repo_returning(wallet, debit_calls=debit_calls),
    )
    resets_at = datetime(2026, 7, 25, 0, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    check = DailyLimitCheck(
        exceeded=True, limit_paise=2_500_000, used_paise=2_489_000, resets_at=resets_at
    )
    monkeypatch.setattr(
        "app.api.routes.payments.PaymentRepo", _payment_repo(check_daily_limit_result=check)
    )

    resp = client.post(
        "/v1/payments", headers=_AUTH_HEADERS, json=_create_payment_body(amount_paise=24_500)
    )

    body = resp.json()
    assert resp.status_code == 402
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"]["code"] == "DAILY_LIMIT_EXCEEDED"
    assert body["error"]["message"] == (
        "Daily transaction limit exceeded: limit ₹25,000, ₹24,890 already used today."
    )
    details = body["error"]["details"]
    assert details["attempted_paise"] == 24_500
    assert details["limit_paise"] == 2_500_000
    assert details["used_today_paise"] == 2_489_000
    assert details["resets_at"] == "2026-07-25T00:00:00+05:30"
    assert details["txn_id"].startswith("txn_")
    assert debit_calls == []
    assert spy_db.committed is True


def test_create_payment_no_wallet_is_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.api.routes.payments.WalletRepo", _wallet_repo_returning(None))

    resp = client.post("/v1/payments", headers=_AUTH_HEADERS, json=_create_payment_body())

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_create_payment_invalid_type_is_400_validation_schema(client: TestClient) -> None:
    resp = client.post(
        "/v1/payments", headers=_AUTH_HEADERS, json=_create_payment_body(type="not_a_real_type")
    )

    body = resp.json()
    assert resp.status_code == 400
    assert body["error"]["code"] == "VALIDATION_SCHEMA"
    assert body["error"]["details"]["fields"]


def test_create_payment_rate_limited_returns_429(monkeypatch: pytest.MonkeyPatch) -> None:
    """`require_rate_limit` (app/api/deps.py) actually firing on the
    mutating endpoint surfaces as a 429 with the docs/13 §1.1 `Retry-After`
    header — exercised end to end through `ErrorEnvelopeMiddleware`,
    unlike test_deps.py's direct unit test of the dependency itself."""
    monkeypatch.setenv("JWT_SECRET", _JWT_SECRET)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    get_settings.cache_clear()

    async def _override_get_db() -> AsyncIterator[_SpySession]:
        yield _SpySession()

    async def _raise_rate_limited() -> None:
        raise RateLimitedError(retry_after=60)

    fastapi_app = _build_app_with_overrides(
        get_db_override=_override_get_db, rate_limit_override=_raise_rate_limited
    )

    with TestClient(fastapi_app) as test_client:
        resp = test_client.post(
            "/v1/payments", headers=_AUTH_HEADERS, json=_create_payment_body()
        )

    get_settings.cache_clear()

    body = resp.json()
    assert resp.status_code == 429
    assert body["error"]["code"] == "RATE_LIMITED"
    assert resp.headers["Retry-After"] == "60"


# --------------------------------------------------------------------------
# POST /v1/limits/increase-requests (docs/13 §3.4)
# --------------------------------------------------------------------------


def test_create_increase_request_returns_201(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = LimitIncreaseRequest(request_id="LMT-2026-0724-0913", status="submitted", eta_hours=4)
    monkeypatch.setattr("app.api.routes.limits.LimitRepo", _limit_repo(result=result))

    resp = client.post(
        "/v1/limits/increase-requests",
        headers=_AUTH_HEADERS,
        json={
            "limit_type": "daily_txn",
            "current_limit_paise": 2_500_000,
            "requested_limit_paise": 5_000_000,
        },
    )

    body = resp.json()
    assert resp.status_code == 201
    assert body["success"] is True
    assert body["data"] == {
        "request_id": "LMT-2026-0724-0913",
        "status": "submitted",
        "eta_hours": 4,
    }


def test_create_increase_request_propagates_already_pending(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typed error `submit_increase_request` raises (app/data/repositories/
    limit_repo.py) must reach the client unchanged via ErrorEnvelopeMiddleware
    — this route does not catch/re-wrap it (per the task brief: "let them
    propagate to the error middleware")."""
    error = LimitRequestAlreadyPendingError(
        "A limit-increase request is already pending",
        details={"existing_request_id": "LMT-2026-0724-0800", "status": "submitted"},
    )
    monkeypatch.setattr("app.api.routes.limits.LimitRepo", _limit_repo(error=error))

    resp = client.post(
        "/v1/limits/increase-requests",
        headers=_AUTH_HEADERS,
        json={
            "limit_type": "daily_txn",
            "current_limit_paise": 2_500_000,
            "requested_limit_paise": 5_000_000,
        },
    )

    body = resp.json()
    assert resp.status_code == 409
    assert body["error"]["code"] == "LIMIT_REQUEST_ALREADY_PENDING"
    assert body["error"]["details"]["existing_request_id"] == "LMT-2026-0724-0800"


# --------------------------------------------------------------------------
# Auth (401) — every protected route shares the same AuthMiddleware, so
# one representative route (GET /v1/wallet) stands in for all of them.
# --------------------------------------------------------------------------


def test_protected_route_without_token_is_401(client: TestClient) -> None:
    resp = client.get("/v1/wallet")

    body = resp.json()
    assert resp.status_code == 401
    assert body["error"]["code"] == "AUTH_MISSING_TOKEN"


def test_protected_route_with_garbage_token_is_401(client: TestClient) -> None:
    resp = client.get("/v1/wallet", headers={"Authorization": "Bearer not-a-real-jwt"})

    body = resp.json()
    assert resp.status_code == 401
    assert body["error"]["code"] == "AUTH_INVALID_TOKEN"


# --------------------------------------------------------------------------
# GET /health
# --------------------------------------------------------------------------


def test_health_requires_no_auth_and_is_not_enveloped(client: TestClient) -> None:
    resp = client.get("/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
