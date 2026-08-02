"""SQLAlchemy 2.0 async-style declarative ORM models for the 8 Phase-2
tables (docs/12-data-models.md §3-4 — the business zone's `merchants`,
`wallet_accounts`, `merchant_limits`, `transactions`, plus the agent
zone's `conversations`, `conversation_turns`, `tool_invocations`,
`call_costs`). The vector zone (`kb_articles`, `memory_chunks`) and the
remaining business tables (`settlements`, `device_orders`, `complaints`,
`cards`) are out of scope for Phase 2 (docs/17 §2.2) and are not modeled
here.

Column names, types, defaults, CHECK constraints, and keys mirror the
docs/12 DDL exactly; constraint *names* (e.g. `ck_merchants_kyc_status`)
are this file's own addition for readable migration diffs — the DDL
itself declares its CHECKs unnamed.

`migrations/env.py` imports `Base` from this module as `target_metadata`;
`migrations/versions/0001_initial_schema.py` hand-writes the matching
`op.create_table` calls rather than autogenerating from this metadata
(docs/12 §10 — autogenerate is a draft, never a commit).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    Text,
    func,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Named as `sa_text`, not `text`, deliberately: ConversationTurn below has a
# column literally named `text` (docs/12 §4.2), and a class-scoped rebind of
# a same-named module import only fails to explode by accident of attribute
# lookup order at class-body-execution time — renaming the import removes
# the landmine instead of relying on that order never changing.

# Alembic autogenerate (future revisions, docs/12 §10 — 0001 itself is
# hand-written) needs deterministic constraint names to diff against;
# without this, unnamed FK/UNIQUE/PK constraints get dialect-assigned names
# that can make autogenerate propose spurious rename/drop-recreate ops.
_NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for every mapped class in this module. Its
    `.metadata` is what `migrations/env.py` sets as `target_metadata`."""

    metadata = MetaData(naming_convention=_NAMING_CONVENTION)


# --------------------------------------------------------------------------
# Business zone (docs/12 §3)
# --------------------------------------------------------------------------


class Merchant(Base):
    """Mirrors `merchants` (docs/12 §3.1) — the seeded business zone's
    root entity every merchant-scoped FK hangs off."""

    __tablename__ = "merchants"
    __table_args__ = (
        CheckConstraint(
            "account_type IN ('Merchant Basic', 'Merchant Pro')",
            name="ck_merchants_account_type",
        ),
        CheckConstraint(
            "kyc_status IN ('pending', 'verified', 'on_hold')",
            name="ck_merchants_kyc_status",
        ),
    )

    merchant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    business_name: Mapped[str] = mapped_column(Text, nullable=False)
    city: Mapped[str] = mapped_column(Text, nullable=False)
    account_type: Mapped[str] = mapped_column(Text, nullable=False)
    preferred_language: Mapped[str] = mapped_column(Text, nullable=False, server_default="English")
    merchant_since: Mapped[date] = mapped_column(Date, nullable=False)
    kyc_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="verified")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    wallet: Mapped[WalletAccount | None] = relationship(back_populates="merchant", uselist=False)


class WalletAccount(Base):
    """Mirrors `wallet_accounts` (docs/12 §3.1). `UNIQUE` on `merchant_id`
    encodes today's one-wallet-per-merchant product fact."""

    __tablename__ = "wallet_accounts"
    __table_args__ = (
        CheckConstraint("balance_paise >= 0", name="ck_wallet_accounts_balance_paise"),
        CheckConstraint("currency = 'INR'", name="ck_wallet_accounts_currency"),
    )

    wallet_id: Mapped[str] = mapped_column(Text, primary_key=True)
    merchant_id: Mapped[str] = mapped_column(
        Text, ForeignKey("merchants.merchant_id"), nullable=False, unique=True
    )
    balance_paise: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False, server_default="INR")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    merchant: Mapped[Merchant] = relationship(back_populates="wallet")


class MerchantLimit(Base):
    """Mirrors `merchant_limits` (docs/12 §3.2) — the table the demo
    pivots on. `request_id`/`requested_paise`/`request_status`/
    `requested_at` fold the (demo-simplified) active limit-increase
    request into this row instead of a separate request table."""

    __tablename__ = "merchant_limits"
    __table_args__ = (
        CheckConstraint(
            "limit_type IN ('daily_txn', 'per_txn')", name="ck_merchant_limits_limit_type"
        ),
        CheckConstraint("limit_paise > 0", name="ck_merchant_limits_limit_paise"),
        CheckConstraint("used_paise >= 0", name="ck_merchant_limits_used_paise"),
        CheckConstraint("requested_paise > limit_paise", name="ck_merchant_limits_requested_paise"),
        CheckConstraint(
            "request_status IN ('submitted', 'approved', 'rejected')",
            name="ck_merchant_limits_request_status",
        ),
        CheckConstraint(
            # 4-way pairing, not just request_id/request_status: Postgres
            # CHECKs pass vacuously on NULL, so a 2-way pairing alone would
            # let requested_paise/requested_at stay NULL on a "submitted"
            # request row — a phantom limit-increase request with no
            # amount, on the table the demo pivots on.
            "(request_id IS NULL) = (request_status IS NULL)"
            " AND (request_status IS NULL) = (requested_paise IS NULL)"
            " AND (requested_paise IS NULL) = (requested_at IS NULL)",
            name="ck_merchant_limits_request_pair",
        ),
    )

    merchant_id: Mapped[str] = mapped_column(
        Text, ForeignKey("merchants.merchant_id"), primary_key=True
    )
    limit_type: Mapped[str] = mapped_column(Text, primary_key=True)
    limit_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    used_paise: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    window_date: Mapped[date] = mapped_column(Date, nullable=False)
    request_id: Mapped[str | None] = mapped_column(Text, unique=True)
    requested_paise: Mapped[int | None] = mapped_column(BigInteger)
    request_status: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Transaction(Base):
    """Mirrors `transactions` (docs/12 §3.3) — what `get_transactions`
    and `get_payment_status` read; `idx_txn_merchant_time` serves their
    one query shape ("this merchant, most recent first")."""

    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint(
            "type IN ('vendor_payment', 'qr_collection', 'settlement_credit', 'refund')",
            name="ck_transactions_type",
        ),
        CheckConstraint("amount_paise > 0", name="ck_transactions_amount_paise"),
        CheckConstraint(
            "status IN ('succeeded', 'declined', 'pending', 'refunded')",
            name="ck_transactions_status",
        ),
        CheckConstraint(
            "(status = 'declined') = (decline_code IS NOT NULL)",
            name="ck_transactions_decline_code_pair",
        ),
        Index("idx_txn_merchant_time", "merchant_id", sa_text("created_at DESC")),
    )

    txn_id: Mapped[str] = mapped_column(Text, primary_key=True)
    merchant_id: Mapped[str] = mapped_column(
        Text, ForeignKey("merchants.merchant_id"), nullable=False
    )
    wallet_id: Mapped[str] = mapped_column(
        Text, ForeignKey("wallet_accounts.wallet_id"), nullable=False
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    counterparty: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    decline_code: Mapped[str | None] = mapped_column(Text)
    http_status: Mapped[int | None] = mapped_column(SmallInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# --------------------------------------------------------------------------
# Agent zone (docs/12 §4)
# --------------------------------------------------------------------------


class Conversation(Base):
    """Mirrors `conversations` (docs/12 §4.1) — the session anchor row,
    written by agent-api at session creation; `state` mirrors the Redis
    `session:{id}` hash's state machine and is updated once, at hang-up."""

    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint(
            "state IN ('created', 'in_call', 'wrap_up', 'ended')", name="ck_conversations_state"
        ),
    )

    session_id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(Text, ForeignKey("merchants.merchant_id"), nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="created")
    signaling_token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConversationTurn(Base):
    """Mirrors `conversation_turns` (docs/12 §4.2) — per-turn metrics,
    not transcript: `text` is always NULL in the Phase-2 demo (the
    transcript-non-persistence decision docs/12 §4.2 documents)."""

    __tablename__ = "conversation_turns"
    __table_args__ = (
        CheckConstraint("turn_no > 0", name="ck_conversation_turns_turn_no"),
        CheckConstraint("role IN ('user', 'agent')", name="ck_conversation_turns_role"),
    )

    session_id: Mapped[str] = mapped_column(
        Text, ForeignKey("conversations.session_id"), primary_key=True
    )
    turn_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    tool_calls: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=sa_text("'{}'")
    )
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    trace_id: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ToolInvocation(Base):
    """Mirrors `tool_invocations` (docs/12 §4.4) — the one during-call
    Postgres write, made synchronously on the tool path. `tool_name` is
    `TEXT` (no CHECK): the allowlist lives in `ToolExecutor`, where a
    non-existent tool produces a typed refusal instead of an insert
    failure that would erase its own evidence."""

    __tablename__ = "tool_invocations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ok', 'error', 'denied', 'pending_confirm', 'cancelled')",
            name="ck_tool_invocations_status",
        ),
        CheckConstraint(
            "(status = 'error') = (error_code IS NOT NULL)",
            name="ck_tool_invocations_error_code_pair",
        ),
        Index("idx_tool_session", "session_id", "created_at"),
    )

    invocation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=sa_text("gen_random_uuid()")
    )
    session_id: Mapped[str] = mapped_column(
        Text, ForeignKey("conversations.session_id"), nullable=False
    )
    turn_no: Mapped[int | None] = mapped_column(Integer)
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    screen_ctx: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error_code: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CallCost(Base):
    """Mirrors `call_costs` (docs/12 §4.5) — the per-call cost ledger.
    The `total_usd` CHECK keeps the aggregate and the per-component
    drill-down from ever disagreeing; unit prices live in config, not
    in this table."""

    __tablename__ = "call_costs"
    __table_args__ = (
        CheckConstraint(
            "total_usd = stt_usd + llm_dialogue_usd + llm_utility_usd + embeddings_usd"
            " + tts_usd + turn_infra_usd",
            name="ck_call_costs_total_usd",
        ),
    )

    session_id: Mapped[str] = mapped_column(
        Text, ForeignKey("conversations.session_id"), primary_key=True
    )
    stt_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    llm_dialogue_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    llm_utility_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    embeddings_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    tts_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    turn_infra_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, server_default="0"
    )
    total_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    stt_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cached_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    tts_chars: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
