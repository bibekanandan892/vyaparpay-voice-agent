"""Unit tests for `scripts/seed.py` (docs/12-data-models.md §8).

No Docker Desktop is available in this environment, so — matching
`backend/tests/data/repositories/test_repositories.py`'s own approach —
these tests never touch a real Postgres. They drive the script's logic
against `unittest.mock` fakes of the repos it calls
(`AsyncMock(spec=MerchantRepo)` etc.) and, only where no repo exposes the
operation (the `--reset` deletes), a `spec=AsyncSession` fake — the same
repo-method boundary `test_repositories.py` mocks at, one layer up.

These tests were executed locally against a throwaway venv (`pip install
-e ".[dev]"`, no Docker required) — see the task report for the pytest
run output.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.repositories import LimitRepo, MerchantRepo, PaymentRepo, WalletRepo
from app.models import Merchant, MerchantLimit, Transaction, WalletAccount
from scripts import seed as seed_module

_IST = timezone(timedelta(hours=5, minutes=30))


def make_session() -> AsyncSession:
    return AsyncMock(spec=AsyncSession)


# --------------------------------------------------------------------------
# Fixture constants pin the docs/01 §6-7 / docs/12 §8 facts this script
# promises to write — a regression here is a regression in the demo
# script's whole reason to exist.
# --------------------------------------------------------------------------


def test_fixture_constants_match_the_canonical_incident() -> None:
    assert seed_module.MERCHANT_ID == "usr_rajesh01"
    assert seed_module.BUSINESS_NAME == "Kumar General Store"
    assert seed_module.CITY == "Jaipur"
    assert seed_module.ACCOUNT_TYPE == "Merchant Pro"
    assert seed_module.PREFERRED_LANGUAGE == "English"

    assert seed_module.WALLET_ID == "wal_rajesh01"
    assert seed_module.WALLET_BALANCE_PAISE == 1_845_000  # ₹18,450

    assert seed_module.LIMIT_TYPE == "daily_txn"
    assert seed_module.DAILY_LIMIT_PAISE == 2_500_000  # ₹25,000
    assert seed_module.DAILY_LIMIT_USED_PAISE == 2_489_000  # ₹24,890 -> ₹110 headroom
    assert seed_module.LIMIT_WINDOW_DATE == date(2026, 7, 24)

    assert seed_module.TXN_ID == "txn_0724_1414a"
    assert seed_module.TXN_TYPE == "vendor_payment"
    assert seed_module.TXN_AMOUNT_PAISE == 24_500  # ₹245
    assert seed_module.TXN_COUNTERPARTY == "Amazon Business"
    assert seed_module.TXN_STATUS == "declined"
    assert seed_module.TXN_DECLINE_CODE == "DAILY_LIMIT_EXCEEDED"
    assert seed_module.TXN_HTTP_STATUS == 402
    assert seed_module.TXN_CREATED_AT == datetime(2026, 7, 24, 14, 14, 0, tzinfo=_IST)


# --------------------------------------------------------------------------
# _seed_merchant / _seed_wallet / _seed_limit / _seed_transaction —
# idempotency (check-then-insert) at the repo-method boundary
# --------------------------------------------------------------------------


async def test_seed_merchant_inserts_when_absent() -> None:
    repo = AsyncMock(spec=MerchantRepo)
    repo.get.return_value = None

    created = await seed_module._seed_merchant(repo)

    assert created is True
    repo.get.assert_awaited_once_with(seed_module.MERCHANT_ID)
    repo.add.assert_awaited_once()
    (merchant,), _ = repo.add.call_args
    assert isinstance(merchant, Merchant)
    assert merchant.merchant_id == "usr_rajesh01"
    assert merchant.business_name == "Kumar General Store"
    assert merchant.city == "Jaipur"
    assert merchant.account_type == "Merchant Pro"
    assert merchant.preferred_language == "English"
    assert merchant.merchant_since == date(2022, 1, 1)


async def test_seed_merchant_skips_when_present() -> None:
    repo = AsyncMock(spec=MerchantRepo)
    repo.get.return_value = Merchant(
        merchant_id="usr_rajesh01",
        business_name="Kumar General Store",
        city="Jaipur",
        account_type="Merchant Pro",
        merchant_since=date(2022, 1, 1),
    )

    created = await seed_module._seed_merchant(repo)

    assert created is False
    repo.add.assert_not_awaited()


async def test_seed_wallet_inserts_when_absent() -> None:
    repo = AsyncMock(spec=WalletRepo)
    repo.get.return_value = None

    created = await seed_module._seed_wallet(repo)

    assert created is True
    repo.get.assert_awaited_once_with(seed_module.WALLET_ID)
    (wallet,), _ = repo.add.call_args
    assert isinstance(wallet, WalletAccount)
    assert wallet.wallet_id == "wal_rajesh01"
    assert wallet.merchant_id == "usr_rajesh01"
    assert wallet.balance_paise == 1_845_000


async def test_seed_wallet_skips_when_present() -> None:
    repo = AsyncMock(spec=WalletRepo)
    repo.get.return_value = WalletAccount(
        wallet_id="wal_rajesh01", merchant_id="usr_rajesh01", balance_paise=1_845_000
    )

    created = await seed_module._seed_wallet(repo)

    assert created is False
    repo.add.assert_not_awaited()


async def test_seed_limit_inserts_when_absent() -> None:
    repo = AsyncMock(spec=LimitRepo)
    repo.get_by_key.return_value = None

    created = await seed_module._seed_limit(repo)

    assert created is True
    repo.get_by_key.assert_awaited_once_with(seed_module.MERCHANT_ID, seed_module.LIMIT_TYPE)
    (limit_row,), _ = repo.add.call_args
    assert isinstance(limit_row, MerchantLimit)
    assert limit_row.merchant_id == "usr_rajesh01"
    assert limit_row.limit_type == "daily_txn"
    assert limit_row.limit_paise == 2_500_000
    assert limit_row.used_paise == 2_489_000
    assert limit_row.window_date == date(2026, 7, 24)


async def test_seed_limit_skips_when_present() -> None:
    repo = AsyncMock(spec=LimitRepo)
    repo.get_by_key.return_value = MerchantLimit(
        merchant_id="usr_rajesh01",
        limit_type="daily_txn",
        limit_paise=2_500_000,
        used_paise=2_489_000,
        window_date=date(2026, 7, 24),
    )

    created = await seed_module._seed_limit(repo)

    assert created is False
    repo.add.assert_not_awaited()


async def test_seed_transaction_inserts_when_absent() -> None:
    repo = AsyncMock(spec=PaymentRepo)
    repo.get.return_value = None

    created = await seed_module._seed_transaction(repo)

    assert created is True
    repo.get.assert_awaited_once_with(seed_module.TXN_ID)
    # repo.create() is deliberately NOT used (no created_at parameter) —
    # regression guard for that decision.
    repo.create.assert_not_awaited()
    (txn,), _ = repo.add.call_args
    assert isinstance(txn, Transaction)
    assert txn.txn_id == "txn_0724_1414a"
    assert txn.merchant_id == "usr_rajesh01"
    assert txn.wallet_id == "wal_rajesh01"
    assert txn.type == "vendor_payment"
    assert txn.amount_paise == 24_500
    assert txn.counterparty == "Amazon Business"
    assert txn.status == "declined"
    assert txn.decline_code == "DAILY_LIMIT_EXCEEDED"
    assert txn.http_status == 402
    assert txn.created_at == datetime(2026, 7, 24, 14, 14, 0, tzinfo=_IST)


async def test_seed_transaction_skips_when_present() -> None:
    repo = AsyncMock(spec=PaymentRepo)
    repo.get.return_value = Transaction(
        txn_id="txn_0724_1414a",
        merchant_id="usr_rajesh01",
        wallet_id="wal_rajesh01",
        type="vendor_payment",
        amount_paise=24_500,
        counterparty="Amazon Business",
        status="declined",
        decline_code="DAILY_LIMIT_EXCEEDED",
        http_status=402,
    )

    created = await seed_module._seed_transaction(repo)

    assert created is False
    repo.add.assert_not_awaited()


# --------------------------------------------------------------------------
# reset_seed_rows — FK-safe deletion order
# --------------------------------------------------------------------------


async def test_reset_seed_rows_deletes_in_fk_safe_order() -> None:
    """Fixed after review (HIGH): the original version only cleared the
    4 business-zone rows. Once a Conversation row exists for this
    merchant (exactly the "demo rehearsal" scenario --reset exists for),
    deleting merchants without first clearing the agent-zone tables that
    reference it (no ON DELETE CASCADE, app/models/orm.py) raises an
    IntegrityError. This pins the full 8-delete, child-to-parent order:
    call_costs/tool_invocations/conversation_turns (all -> conversations)
    -> conversations -> transactions -> merchant_limits -> wallet_accounts
    -> merchants."""
    session = make_session()

    await seed_module.reset_seed_rows(session)

    assert session.execute.await_count == 8
    tables_in_order = [
        str(c.args[0].compile(dialect=postgresql.dialect()))
        for c in session.execute.await_args_list
    ]
    assert "DELETE FROM call_costs" in tables_in_order[0]
    assert "DELETE FROM tool_invocations" in tables_in_order[1]
    assert "DELETE FROM conversation_turns" in tables_in_order[2]
    assert "DELETE FROM conversations" in tables_in_order[3]
    assert "DELETE FROM transactions" in tables_in_order[4]
    assert "DELETE FROM merchant_limits" in tables_in_order[5]
    assert "DELETE FROM wallet_accounts" in tables_in_order[6]
    assert "DELETE FROM merchants" in tables_in_order[7]
    session.flush.assert_awaited_once()


async def test_reset_seed_rows_scopes_deletes_to_the_seeded_keys() -> None:
    """Review-guard: this must never compile to an unqualified
    `DELETE FROM merchants` with no WHERE — that would nuke every
    merchant, not just the seeded one, on a database that has grown
    other data since seeding."""
    session = make_session()

    await seed_module.reset_seed_rows(session)

    compiled = [
        c.args[0].compile(dialect=postgresql.dialect()) for c in session.execute.await_args_list
    ]
    # The 3 agent-zone deletes (call_costs, tool_invocations,
    # conversation_turns) are scoped via an `IN (SELECT session_id FROM
    # conversations WHERE user_id = :user_id_1)` subquery, not a direct
    # column on those tables (none of them carry merchant_id) — the
    # subquery's own WHERE clause is what's actually scoped.
    for i in range(3):
        assert compiled[i].params["user_id_1"] == "usr_rajesh01"
    assert compiled[3].params["user_id_1"] == "usr_rajesh01"  # DELETE FROM conversations itself
    assert compiled[4].params["txn_id_1"] == "txn_0724_1414a"
    assert compiled[5].params["merchant_id_1"] == "usr_rajesh01"
    assert compiled[5].params["limit_type_1"] == "daily_txn"
    assert compiled[6].params["wallet_id_1"] == "wal_rajesh01"
    assert compiled[7].params["merchant_id_1"] == "usr_rajesh01"


# --------------------------------------------------------------------------
# _seed_all — orchestration: all four rows attempted, one commit
# --------------------------------------------------------------------------


async def test_seed_all_commits_once_after_all_four_rows() -> None:
    session = make_session()
    merchant_repo = AsyncMock(spec=MerchantRepo)
    wallet_repo = AsyncMock(spec=WalletRepo)
    limit_repo = AsyncMock(spec=LimitRepo)
    payment_repo = AsyncMock(spec=PaymentRepo)
    merchant_repo.get.return_value = None
    wallet_repo.get.return_value = None
    limit_repo.get_by_key.return_value = None
    payment_repo.get.return_value = None

    summary = await seed_module._seed_all(
        merchant_repo=merchant_repo,
        wallet_repo=wallet_repo,
        limit_repo=limit_repo,
        payment_repo=payment_repo,
        session=session,
    )

    assert summary == seed_module.SeedSummary(
        merchant_created=True,
        wallet_created=True,
        limit_created=True,
        transaction_created=True,
    )
    session.commit.assert_awaited_once()


async def test_seed_all_reports_mixed_created_and_existing_rows() -> None:
    session = make_session()
    merchant_repo = AsyncMock(spec=MerchantRepo)
    wallet_repo = AsyncMock(spec=WalletRepo)
    limit_repo = AsyncMock(spec=LimitRepo)
    payment_repo = AsyncMock(spec=PaymentRepo)
    merchant_repo.get.return_value = Merchant(
        merchant_id="usr_rajesh01",
        business_name="Kumar General Store",
        city="Jaipur",
        account_type="Merchant Pro",
        merchant_since=date(2022, 1, 1),
    )
    wallet_repo.get.return_value = None
    limit_repo.get_by_key.return_value = None
    payment_repo.get.return_value = None

    summary = await seed_module._seed_all(
        merchant_repo=merchant_repo,
        wallet_repo=wallet_repo,
        limit_repo=limit_repo,
        payment_repo=payment_repo,
        session=session,
    )

    assert summary.merchant_created is False
    assert summary.wallet_created is True
    assert summary.limit_created is True
    assert summary.transaction_created is True


async def test_seed_all_does_not_commit_when_a_row_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review finding (MEDIUM): the module docstring promises a failure
    partway through never reaches commit() — this was previously
    unverified by any test, even though it's the exact property `seed()`
    exists to guarantee (a failed re-insert after --reset must roll the
    delete back too, never leaving the fixture set half-erased)."""
    session = make_session()
    merchant_repo = AsyncMock(spec=MerchantRepo)
    wallet_repo = AsyncMock(spec=WalletRepo)
    limit_repo = AsyncMock(spec=LimitRepo)
    payment_repo = AsyncMock(spec=PaymentRepo)
    merchant_repo.get.return_value = None
    wallet_repo.get.side_effect = RuntimeError("simulated mid-sequence failure")

    with pytest.raises(RuntimeError, match="simulated mid-sequence failure"):
        await seed_module._seed_all(
            merchant_repo=merchant_repo,
            wallet_repo=wallet_repo,
            limit_repo=limit_repo,
            payment_repo=payment_repo,
            session=session,
        )

    session.commit.assert_not_awaited()
    limit_repo.get_by_key.assert_not_awaited()  # never reached either — the failure stops the pass
    payment_repo.get.assert_not_awaited()


# --------------------------------------------------------------------------
# seed() — top-level: reset gate + delegates to _seed_all
# --------------------------------------------------------------------------


async def test_seed_skips_reset_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    session = make_session()
    reset_mock = AsyncMock()
    seed_all_mock = AsyncMock(return_value="the-summary")
    monkeypatch.setattr(seed_module, "reset_seed_rows", reset_mock)
    monkeypatch.setattr(seed_module, "_seed_all", seed_all_mock)

    result = await seed_module.seed(session)

    reset_mock.assert_not_awaited()
    seed_all_mock.assert_awaited_once()
    assert result == "the-summary"


async def test_seed_calls_reset_when_flag_set(monkeypatch: pytest.MonkeyPatch) -> None:
    session = make_session()
    reset_mock = AsyncMock()
    seed_all_mock = AsyncMock(return_value="the-summary")
    monkeypatch.setattr(seed_module, "reset_seed_rows", reset_mock)
    monkeypatch.setattr(seed_module, "_seed_all", seed_all_mock)

    result = await seed_module.seed(session, reset=True)

    reset_mock.assert_awaited_once_with(session)
    seed_all_mock.assert_awaited_once()
    assert result == "the-summary"


# --------------------------------------------------------------------------
# SeedSummary.render() — the demo-facing summary text
# --------------------------------------------------------------------------


def test_seed_summary_render_marks_every_row_seeded() -> None:
    summary = seed_module.SeedSummary(
        merchant_created=True, wallet_created=True, limit_created=True, transaction_created=True
    )

    text = summary.render()

    assert "usr_rajesh01" in text
    assert "wal_rajesh01" in text
    assert "usr_rajesh01/daily_txn" in text
    assert "txn_0724_1414a" in text
    assert text.count("seeded") == 4
    assert "already present" not in text


def test_seed_summary_render_marks_existing_rows_distinctly() -> None:
    summary = seed_module.SeedSummary(
        merchant_created=False,
        wallet_created=False,
        limit_created=False,
        transaction_created=False,
    )

    text = summary.render()

    assert text.count("already present") == 4
    assert "[seeded" not in text


# --------------------------------------------------------------------------
# CLI wiring — argument parsing and the async entry point
# --------------------------------------------------------------------------


def test_parse_args_defaults_reset_to_false() -> None:
    args = seed_module._parse_args([])
    assert args.reset is False


def test_parse_args_reset_flag() -> None:
    args = seed_module._parse_args(["--reset"])
    assert args.reset is True


class _FakeSessionContext:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _FakeSettings:
    """Real `Settings` has far more fields than `_amain` touches; this
    stands in for just the one it reads directly (`env`, for the
    `--reset` guard below) without pulling in the whole pydantic-settings
    machinery (which would need real env vars for its required fields)."""

    def __init__(self, env: str = "dev") -> None:
        self.env = env


async def test_amain_wires_settings_engine_session_and_disposes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_settings = _FakeSettings(env="dev")
    fake_session = make_session()
    fake_engine = AsyncMock()
    sessionmaker_calls: list[None] = []

    def fake_sessionmaker() -> _FakeSessionContext:
        sessionmaker_calls.append(None)
        return _FakeSessionContext(fake_session)

    seed_mock = AsyncMock(
        return_value=seed_module.SeedSummary(
            merchant_created=True, wallet_created=True, limit_created=True, transaction_created=True
        )
    )

    monkeypatch.setattr(seed_module, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(
        seed_module,
        "create_engine_and_sessionmaker",
        lambda settings: (fake_engine, fake_sessionmaker),
    )
    monkeypatch.setattr(seed_module, "seed", seed_mock)

    await seed_module._amain(["--reset"])

    assert len(sessionmaker_calls) == 1
    seed_mock.assert_awaited_once_with(fake_session, reset=True)
    fake_engine.dispose.assert_awaited_once()
    captured = capsys.readouterr()
    assert "VyaparPay demo fixtures" in captured.out


async def test_amain_disposes_engine_even_when_seed_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_session = make_session()
    fake_engine = AsyncMock()

    def fake_sessionmaker() -> _FakeSessionContext:
        return _FakeSessionContext(fake_session)

    monkeypatch.setattr(seed_module, "get_settings", lambda: object())
    monkeypatch.setattr(
        seed_module,
        "create_engine_and_sessionmaker",
        lambda settings: (fake_engine, fake_sessionmaker),
    )
    monkeypatch.setattr(seed_module, "seed", AsyncMock(side_effect=RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        await seed_module._amain([])

    fake_engine.dispose.assert_awaited_once()


@pytest.mark.parametrize("env", ["prod", "production", "staging"])
async def test_amain_refuses_reset_outside_dev_test(
    monkeypatch: pytest.MonkeyPatch, env: str
) -> None:
    """Review finding (MEDIUM, security): --reset had no rail against
    running with whatever DATABASE_URL happens to be set — e.g. a
    copy-pasted prod connection string in a local .env during a demo
    rehearsal. Defense-in-depth only (the deletes are already scoped to
    one merchant's rows, never a wildcard) but this is the one place
    settings.env is actually load-bearing."""
    monkeypatch.setattr(seed_module, "get_settings", lambda: _FakeSettings(env=env))
    engine_and_sessionmaker_called = AsyncMock()
    monkeypatch.setattr(
        seed_module,
        "create_engine_and_sessionmaker",
        lambda settings: engine_and_sessionmaker_called(),
    )

    with pytest.raises(SystemExit, match=env):
        await seed_module._amain(["--reset"])

    engine_and_sessionmaker_called.assert_not_called()  # refused before touching the DB at all


@pytest.mark.parametrize("env", ["dev", "test"])
async def test_amain_allows_reset_in_dev_and_test(
    monkeypatch: pytest.MonkeyPatch, env: str
) -> None:
    fake_session = make_session()
    fake_engine = AsyncMock()

    def fake_sessionmaker() -> _FakeSessionContext:
        return _FakeSessionContext(fake_session)

    monkeypatch.setattr(seed_module, "get_settings", lambda: _FakeSettings(env=env))
    monkeypatch.setattr(
        seed_module,
        "create_engine_and_sessionmaker",
        lambda settings: (fake_engine, fake_sessionmaker),
    )
    monkeypatch.setattr(
        seed_module,
        "seed",
        AsyncMock(
            return_value=seed_module.SeedSummary(
                merchant_created=True,
                wallet_created=True,
                limit_created=True,
                transaction_created=True,
            )
        ),
    )

    await seed_module._amain(["--reset"])  # must not raise

    fake_engine.dispose.assert_awaited_once()


async def test_amain_does_not_check_env_when_reset_not_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard is scoped to --reset specifically — a plain seed run
    (no deletes at all) must work regardless of settings.env."""
    fake_session = make_session()
    fake_engine = AsyncMock()

    def fake_sessionmaker() -> _FakeSessionContext:
        return _FakeSessionContext(fake_session)

    monkeypatch.setattr(seed_module, "get_settings", lambda: _FakeSettings(env="prod"))
    monkeypatch.setattr(
        seed_module,
        "create_engine_and_sessionmaker",
        lambda settings: (fake_engine, fake_sessionmaker),
    )
    monkeypatch.setattr(
        seed_module,
        "seed",
        AsyncMock(
            return_value=seed_module.SeedSummary(
                merchant_created=True,
                wallet_created=True,
                limit_created=True,
                transaction_created=True,
            )
        ),
    )

    await seed_module._amain([])  # no --reset, must not raise even with env="prod"

    fake_engine.dispose.assert_awaited_once()
