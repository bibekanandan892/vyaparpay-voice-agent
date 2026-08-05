"""SQLAlchemy 2.0 async-style declarative ORM models for the 8 Phase-2
tables (docs/12-data-models.md §3-4 — the business zone's `merchants`,
`wallet_accounts`, `merchant_limits`, `transactions`, plus the agent
zone's `conversations`, `conversation_turns`, `tool_invocations`,
`call_costs`), plus the 4 Phase-5 memory tables added by migration 0002:
`conversation_summaries` (docs/12 §4.3), `user_profiles` (docs/09 §5.1),
and the vector zone's `kb_articles` + `memory_chunks` (docs/12 §5,
docs/09 §6.1). The remaining business tables (`settlements`,
`device_orders`, `complaints`, `cards`) are still unmodeled — no Phase-5
task reads them.

Four of these classes (`ConversationSummary`, `UserProfile`, `KbArticle`,
`MemoryChunk`) share a name with a frozen pydantic value type in
app/domain/types.py. That is deliberate — the row and its domain twin
describe the same thing at different layers — but it means a module
importing both must alias one (`from app.models.orm import UserProfile as
UserProfileRow`); a plain double import silently shadows the first.

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

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    ColumnElement,
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

from app.domain.types import EMBEDDING_DIM, MemoryKind, SessionUser

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


class ConversationSummary(Base):
    """Mirrors `conversation_summaries` (docs/09 §8 owns the schema;
    docs/12 §4.3 reproduces it and adds the FK to `conversations` plus
    `idx_summaries_user`). Written once, post-call, by the agent-api
    persistence pipeline.

    `turn_count`/`duration_s`/`cost_usd` duplicate what `conversation_turns`
    and `call_costs` can derive — deliberate, so the row renders into the
    RAG slot self-contained without a three-way join on the retrieval path.
    `cost_usd` is `NUMERIC(8,4)` here (the rounded display copy) against
    `call_costs`'s `NUMERIC(10,6)` ledger precision — docs/12 §4.5 calls
    that split out explicitly, so the narrower type is not a typo.

    **No `embedding` column, by design.** The vector for this summary is a
    `memory_chunks` row with `kind='call_summary'`, written by the same
    pipeline stage. Vectors stay out of source-of-record tables so that
    re-embedding (model swap, dimension change) rebuilds one derived
    table instead of `ALTER`-ing every table that owns text (docs/12 §4.3).

    Domain twin: `app.domain.types.ConversationSummary`.
    """

    __tablename__ = "conversation_summaries"
    __table_args__ = (
        CheckConstraint(
            "resolution IN ('resolved', 'escalated', 'pending', 'abandoned')",
            name="ck_conversation_summaries_resolution",
        ),
        Index("idx_summaries_user", "user_id", sa_text("created_at DESC")),
    )

    session_id: Mapped[str] = mapped_column(
        Text, ForeignKey("conversations.session_id"), primary_key=True
    )
    # No FK to merchants: docs/12 §4.3's DDL declares this as a plain
    # NOT NULL TEXT. It is still the right-to-delete handle (docs/09 §10,
    # `DELETE WHERE user_id = ?`), which is why it is indexed below.
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    resolution: Mapped[str] = mapped_column(Text, nullable=False)
    intents: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    tools_used: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_s: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UserProfile(Base):
    """Mirrors `user_profiles` (docs/09 §5.1 — the owning doc per canon
    §11; docs/12 does not reproduce this DDL). Memory layer 5: durable,
    merge-updated post-call, read once at call setup into the 200-token
    profile slot.

    **No FK on `user_id`.** docs/09 §5.1 declares `user_id TEXT PRIMARY
    KEY` with no `REFERENCES merchants(merchant_id)`, and docs/09 wins for
    this table. Noted because docs/12 §1's ER diagram draws
    `merchants ||--|| user_profiles "profiled as"`, which reads as an
    enforced 1:1 — neither doc's DDL enforces it, and this model does not
    add it. Consequence, stated rather than hidden: a profile row can
    outlive or precede its merchant row, and nothing at the DB level
    prevents a profile for a merchant_id that never existed.

    **The three JSONB columns are unvalidated at this layer.** docs/09
    §5.2's closed extraction schema (`UserProfileFacts` /
    `UserProfilePreferences` / `OpenIssue` in app/domain/types.py) is a
    Pydantic-boundary control applied by the post-call merge; Postgres
    will accept any JSON shape in these columns. docs/12 §6's JSONB policy
    accepts that trade because these rows are read whole and never
    filtered by inner fields on a hot path — but "validated by Pydantic"
    is a property of the write path, not of this table.

    Domain twin: `app.domain.types.UserProfile`.
    """

    __tablename__ = "user_profiles"

    user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    facts: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa_text("'{}'")
    )
    preferences: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa_text("'{}'")
    )
    open_issues: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa_text("'[]'")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_by_call: Mapped[str | None] = mapped_column(Text)


# --------------------------------------------------------------------------
# Vector zone (docs/12 §5, docs/09 §6.1)
# --------------------------------------------------------------------------


class KbArticle(Base):
    """Mirrors `kb_articles` (docs/12 §5) — the **source of record** for
    support knowledge, written by the seed script at deploy time.

    **No `embedding` column, by design.** A ~40-article KB chunks into
    ~180 heading-aware ~300-token pieces, and retrieval quality lives at
    chunk granularity: one article-level vector averages away exactly the
    section the query wanted. The derived vectors live in `memory_chunks`,
    so re-chunking or re-embedding is `DELETE WHERE kind = 'kb_article'`
    plus a re-run of the seed embedder — `kb_articles` itself never
    changes (docs/12 §5).

    Domain twin: `app.domain.types.KbArticle`.
    """

    __tablename__ = "kb_articles"
    __table_args__ = (
        CheckConstraint(
            "category IN ('limits', 'settlements', 'refunds', 'devices', 'kyc')",
            name="ck_kb_articles_category",
        ),
    )

    slug: Mapped[str] = mapped_column(Text, primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body_md: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MemoryChunk(Base):
    """Mirrors `memory_chunks` (docs/09 §6.1 owns it per canon §11; docs/12
    §5 reproduces it and names the index `idx_chunks_embedding`). The
    **derived** embedding store for both KB chunks and call summaries,
    discriminated by `kind`, sharing one table and one HNSW cosine index.

    `source_id` is polymorphic — `kb_articles.slug` when `kind =
    'kb_article'`, `conversations.session_id` when `kind = 'call_summary'`
    — so it carries **no FK**: a column cannot reference two tables. Both
    docs' DDL leave it unconstrained for the same reason. Consequence: a
    dangling `source_id` is representable, and only the seed/post-call
    writers keep it honest.

    **`ck_memory_chunks_user_scope` is this file's addition, and it is the
    important one.** Both docs annotate the column `-- NULL for KB;
    REQUIRED for call_summary (scoping)`, but **neither doc's DDL declares
    any constraint enforcing it** — the comment claimed more than the SQL
    delivered. The retrieval predicate docs/09 §6.2 pins as a security
    invariant is `kind = 'kb_article' OR user_id = :session_user`, and it
    is only sound if the two halves of that comment actually hold, so the
    biconditional is declared here rather than trusted:

    - a `call_summary` with `user_id IS NULL` matches no merchant at all
      (`NULL = 'usr_x'` is NULL, not true) — the summary is silently
      unreachable by its own owner, memory loss that no error reports;
    - a `kb_article` carrying a `user_id` is the mirror-image bug — a row
      that is globally readable via the `kind` half of the predicate while
      being labelled as if it were merchant-private.

    The same biconditional is re-checked in
    `app.domain.types.MemoryChunk`, so a bad row is rejected at whichever
    boundary it reaches first. Neither check is the tenant-scoping control
    itself — that is the WHERE clause in Batch 2's retriever. This one
    only guarantees the data the WHERE clause reasons about is
    well-formed.

    **HNSW over ivfflat**: ivfflat needs training on a representative
    sample and list-count retuning as the corpus grows — wrong assumptions
    for a table starting at ~180 chunks and growing one row per call —
    while HNSW builds incrementally with better recall at small/streaming
    sizes. Its costs (slower writes, more memory) are irrelevant at one
    insert per call. Honest caveat from docs/09 §6.1 and docs/12 §5: at
    ~200 rows this index is decoration, a sequential scan is
    sub-millisecond. It exists so the schema is production-shaped.

    Domain twin: `app.domain.types.MemoryChunk`.
    """

    __tablename__ = "memory_chunks"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('kb_article', 'call_summary')",
            name="ck_memory_chunks_kind",
        ),
        CheckConstraint(
            "(kind = 'call_summary') = (user_id IS NOT NULL)",
            name="ck_memory_chunks_user_scope",
        ),
        CheckConstraint(
            # Separate from the biconditional on purpose: folding
            # `<> ''` into that one would also forbid a kb_article's
            # legitimate NULL, and an OR'd variant would let a kb_article
            # carry ''. Blank is its own failure — `user_id = ''` is not
            # NULL, so it satisfies the biconditional while matching no
            # principal, producing a call summary that looks scoped and is
            # reachable by nobody.
            "user_id IS NULL OR btrim(user_id) <> ''",
            name="ck_memory_chunks_user_id_not_blank",
        ),
        Index(
            "idx_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[Any] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


def memory_scope(principal: SessionUser) -> ColumnElement[bool]:
    """docs/09 §6.2's retrieval scoping predicate — `kind = 'kb_article'
    OR user_id = :session_user` — as one composable clause.

    It exists so the predicate has **one reviewable call site** instead of
    being re-derived at each query. docs/09 §6.2 says the scoping "lives in
    the one function that issues the query"; this is the smaller, testable
    piece of that: a caller composes it with `.where(...)` rather than
    hand-writing an OR whose second branch is easy to drop.

    Requiring `SessionUser` (not a bare `user_id: str`) keeps it on the
    same principal-injection path as the tool layer (docs/10 §1 invariant
    3), so a scope cannot be assembled from an untrusted string at the
    call site.

    **What this is not.** Calling it is not enforced — a query that never
    calls it is still valid Python and valid SQL, and nothing in this
    module can detect one. Making it the *only* path to the table means
    giving `SemanticRepo` sole ownership of `search()` (docs/04 §5); that
    is Batch 2's to build, and this function is what it should compose.

    **Index caveat for the caller.** `memory_chunks.user_id` is
    unindexed, and pgvector's HNSW post-filters: an ANN scan collects
    candidates by vector distance *first*, then this predicate removes the
    ones that don't match. A merchant whose own summaries are all outside
    the top `ef_search` candidates can therefore get zero of their own
    rows back. The correct levers are raising `ef_search`, enabling
    iterative index scans, or adding a partial index. Fetching a larger
    top-k globally and filtering in Python is **not** an acceptable
    workaround: it pulls other merchants' summaries into application
    memory, which is the exact exposure the predicate exists to prevent.
    Irrelevant at the demo's ~200 rows, where the planner picks a
    sequential scan anyway; it matters the moment the corpus grows.
    """
    return (MemoryChunk.kind == MemoryKind.KB_ARTICLE.value) | (
        MemoryChunk.user_id == principal.user_id
    )
