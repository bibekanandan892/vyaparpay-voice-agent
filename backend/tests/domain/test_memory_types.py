"""Value-type tests for the Phase-5 memory domain types
(app/domain/types.py — `ConversationSummary`, `UserProfile`,
`MemoryChunk`, `RetrievedMemory` and the closed extraction schema).

These run in the standard suite: they are pure Pydantic, no database.
They cover the invariants this layer actually enforces — and, where a
docstring claims something the type does *not* enforce, there is a test
pinning that gap honestly rather than a test implying coverage that
doesn't exist (see `test_user_profile_jsonb_shape_is_not_enforced_by_the_row`
in tests/models/test_memory_orm.py for the DB-side counterpart).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.domain.types import (
    EMBEDDING_DIM,
    ConversationSummary,
    MemoryChunk,
    MemoryKind,
    OpenIssue,
    Resolution,
    RetrievedMemory,
    UserProfile,
    UserProfileFacts,
    UserProfilePreferences,
)


def _vector(dim: int = EMBEDDING_DIM) -> tuple[float, ...]:
    return tuple(0.01 for _ in range(dim))


# --------------------------------------------------------------------------
# MemoryChunk — embedding width
# --------------------------------------------------------------------------


def test_memory_chunk_accepts_a_1536_dim_embedding() -> None:
    chunk = MemoryChunk(
        kind=MemoryKind.KB_ARTICLE,
        source_id="kb_daily_limits",
        content="Daily limits reset at midnight IST.",
        embedding=_vector(),
    )

    assert len(chunk.embedding) == EMBEDDING_DIM
    assert chunk.user_id is None
    assert chunk.id is None  # BIGSERIAL, assigned on insert


@pytest.mark.parametrize("dim", [1535, 1537, 3072, 0])
def test_memory_chunk_rejects_a_wrong_width_embedding(dim: int) -> None:
    """The `vector(1536)` column would reject this too, but only after a
    round trip. 3072 is `text-embedding-3-large`'s width — the realistic
    way this goes wrong is a model swap in config that nothing else
    notices."""
    with pytest.raises(ValidationError, match="1536-dimensional"):
        MemoryChunk(
            kind=MemoryKind.KB_ARTICLE,
            source_id="kb_daily_limits",
            content="...",
            embedding=_vector(dim),
        )


# --------------------------------------------------------------------------
# MemoryChunk — the user-scope biconditional (mirrors ck_memory_chunks_user_scope)
# --------------------------------------------------------------------------


def test_call_summary_chunk_requires_a_user_id() -> None:
    """docs/09 §6.1 annotates the column "REQUIRED for call_summary
    (scoping)" but declares no constraint. A NULL `user_id` here makes the
    row unreachable by *every* merchant including its owner, because the
    scoping predicate's `user_id = :session_user` is NULL-false — silent
    memory loss that raises no error at retrieval time."""
    with pytest.raises(ValidationError, match="required when kind='call_summary'"):
        MemoryChunk(
            kind=MemoryKind.CALL_SUMMARY,
            source_id="a1f3c9",
            content="Rajesh's vendor payment was declined...",
            embedding=_vector(),
            user_id=None,
        )


def test_kb_article_chunk_must_not_carry_a_user_id() -> None:
    """The mirror-image bug: a KB chunk is globally readable through the
    `kind = 'kb_article'` half of the predicate, so labelling one with a
    user_id misrepresents a public row as merchant-private."""
    with pytest.raises(ValidationError, match="must be NULL when kind='kb_article'"):
        MemoryChunk(
            kind=MemoryKind.KB_ARTICLE,
            source_id="kb_daily_limits",
            content="...",
            embedding=_vector(),
            user_id="usr_rajesh01",
        )


def test_call_summary_chunk_with_a_user_id_is_valid() -> None:
    chunk = MemoryChunk(
        kind=MemoryKind.CALL_SUMMARY,
        source_id="a1f3c9",
        content="Rajesh's vendor payment was declined...",
        embedding=_vector(),
        user_id="usr_rajesh01",
    )

    assert chunk.user_id == "usr_rajesh01"


# --------------------------------------------------------------------------
# RetrievedMemory — similarity, not distance
# --------------------------------------------------------------------------


def test_retrieved_memory_carries_content_and_score() -> None:
    hit = RetrievedMemory(
        chunk_id=42,
        kind=MemoryKind.KB_ARTICLE,
        source_id="kb_daily_limits",
        content="Daily limits reset at midnight IST.",
        similarity=0.83,
    )

    assert hit.content.startswith("Daily limits")
    assert hit.similarity == pytest.approx(0.83)


@pytest.mark.parametrize("value", [1.0, -1.0, 0.0])
def test_retrieved_memory_accepts_the_inclusive_cosine_bounds(value: float) -> None:
    """The range check is inclusive: 1.0 is a legitimate score (a query
    embedded identically to a stored chunk — which the call-setup prefetch
    can genuinely produce when the same error code was seen before), and
    -1.0 is the legitimate opposite pole. Without this test a validator
    narrowed to strict `<`/`>` would reject a real perfect match and pass
    the rest of the suite unnoticed."""
    hit = RetrievedMemory(
        chunk_id=1,
        kind=MemoryKind.KB_ARTICLE,
        source_id="kb_daily_limits",
        content="...",
        similarity=value,
    )

    assert hit.similarity == pytest.approx(value)


@pytest.mark.parametrize("value", [1.5, -1.2, 42.0])
def test_retrieved_memory_rejects_out_of_range_similarity(value: float) -> None:
    """Cosine similarity lives in [-1, 1]. A value outside it usually means
    pgvector's `<=>` cosine *distance* was passed through unconverted —
    which would invert the 0.70 floor comparison and admit exactly the
    marginal results the floor exists to drop."""
    with pytest.raises(ValidationError, match="cosine similarity"):
        RetrievedMemory(
            chunk_id=1,
            kind=MemoryKind.KB_ARTICLE,
            source_id="kb_daily_limits",
            content="...",
            similarity=value,
        )


def test_retrieved_memory_does_not_carry_the_vector() -> None:
    """ContextBuilder renders these into a ~300-token RAG slot and never
    needs the 1536-float geometry that produced them."""
    assert "embedding" not in RetrievedMemory.model_fields


# --------------------------------------------------------------------------
# Closed extraction schema (docs/09 §5.2)
# --------------------------------------------------------------------------


def test_profile_facts_drop_keys_outside_the_closed_schema() -> None:
    """docs/09 §5.2: "Keys outside the schema are dropped, not stored — the
    allowlist is the defense against the extractor inventing fields."

    `extra="ignore"` (Pydantic's default) is deliberate over
    `extra="forbid"`: forbidding would reject a whole otherwise-good
    extraction because the model volunteered one extra key, whereas
    dropping keeps the good facts and discards the invention. The inferred
    keys below are exactly the docs/14 no-inferred-PII line: a mood, a
    demographic guess, a credit assessment.
    """
    facts = UserProfileFacts.model_validate(
        {
            "business_name": "Kumar General Store",
            "city": "Jaipur",
            "mood": "frustrated",
            "estimated_income": "low",
            "probably_a_small_merchant": True,
        }
    )

    assert facts.business_name == "Kumar General Store"
    assert facts.city == "Jaipur"
    dumped = facts.model_dump()
    assert "mood" not in dumped
    assert "estimated_income" not in dumped
    assert "probably_a_small_merchant" not in dumped


def test_profile_facts_are_all_optional() -> None:
    """A merchant may never state their city, and an absent fact must stay
    absent rather than be guessed at."""
    facts = UserProfileFacts()

    assert facts.business_name is None
    assert facts.city is None


def test_profile_preferences_drop_unknown_keys_too() -> None:
    prefs = UserProfilePreferences.model_validate({"language": "English", "tone": "casual"})

    assert prefs.language == "English"
    assert "tone" not in prefs.model_dump()


# --------------------------------------------------------------------------
# UserProfile / ConversationSummary round trips
# --------------------------------------------------------------------------


def test_user_profile_defaults_to_empty_containers() -> None:
    profile = UserProfile(user_id="usr_rajesh01")

    assert profile.facts == UserProfileFacts()
    assert profile.open_issues == ()
    assert profile.updated_by_call is None


def test_user_profile_carries_open_issues_with_provenance() -> None:
    """`open_issues` is what makes the *next* call feel continuous, and
    `opened_call` is the session that created the issue (docs/09 §5.1)."""
    profile = UserProfile(
        user_id="usr_rajesh01",
        facts=UserProfileFacts(business_name="Kumar General Store", city="Jaipur"),
        preferences=UserProfilePreferences(language="English"),
        open_issues=(
            OpenIssue(
                id="iss_071",
                summary="Daily limit increase requested",
                status="pending",
                opened_call="a1f3c9",
                opened_at=datetime(2026, 7, 24, 8, 59, tzinfo=UTC),
            ),
        ),
        updated_by_call="a1f3c9",
    )

    assert profile.open_issues[0].opened_call == "a1f3c9"
    assert profile.updated_by_call == "a1f3c9"


def test_conversation_summary_round_trip() -> None:
    summary = ConversationSummary(
        session_id="a1f3c9",
        user_id="usr_rajesh01",
        summary="Vendor payment declined - daily limit exceeded.",
        resolution=Resolution.PENDING,
        intents=("payment_failure", "limit_increase"),
        tools_used=("get_wallet_balance", "request_limit_increase"),
        turn_count=15,
        duration_s=214,
        cost_usd=Decimal("0.2841"),
    )

    assert summary.resolution is Resolution.PENDING
    assert summary.cost_usd == Decimal("0.2841")
    assert summary.created_at is None  # server-assigned on insert


def test_conversation_summary_rejects_an_unknown_resolution() -> None:
    """Mirrors `ck_conversation_summaries_resolution` (docs/12 §4.3)."""
    with pytest.raises(ValidationError):
        ConversationSummary(
            session_id="a1f3c9",
            user_id="usr_rajesh01",
            summary="...",
            resolution="half_resolved",  # type: ignore[arg-type]
            turn_count=1,
            duration_s=1,
            cost_usd=Decimal("0"),
        )


def test_conversation_summary_carries_no_embedding_field() -> None:
    """docs/12 §4.3: the vector lives in `memory_chunks`, so re-embedding
    rebuilds one derived table rather than `ALTER`-ing every table that
    owns text."""
    assert "embedding" not in ConversationSummary.model_fields


# --------------------------------------------------------------------------
# Frozen-value discipline (module-wide convention)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("instance", "field"),
    [
        (UserProfile(user_id="usr_rajesh01"), "user_id"),
        (UserProfileFacts(city="Jaipur"), "city"),
        (
            RetrievedMemory(
                chunk_id=1,
                kind=MemoryKind.KB_ARTICLE,
                source_id="kb_daily_limits",
                content="...",
                similarity=0.9,
            ),
            "similarity",
        ),
    ],
)
def test_memory_value_types_are_frozen(instance: object, field: str) -> None:
    """Every value object in app/domain/types.py is immutable — build a new
    instance instead of mutating one."""
    with pytest.raises(ValidationError):
        setattr(instance, field, "mutated")
