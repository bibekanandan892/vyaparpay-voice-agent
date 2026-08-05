"""Value-type tests for the Phase-5 memory domain types
(app/domain/types.py — `ConversationSummary`, `UserProfile`,
`MemoryChunk`, `RetrievedMemory` and the closed extraction schema).

These run in the standard suite: they are pure Pydantic, no database.
They cover the invariants this layer actually enforces — and, where a
docstring claims something the type does *not* enforce, there is a test
pinning that gap honestly rather than a test implying coverage that
doesn't exist (see
`test_user_profile_row_does_not_validate_its_jsonb_shape` in
tests/models/test_memory_orm.py for the DB-side counterpart).
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
    SessionUser,
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


def test_retrieved_memory_identifies_the_owner_of_a_call_summary() -> None:
    """Defense in depth: without `user_id` on the result, a leaked foreign
    summary is undetectable downstream — the caller holds the text but
    nothing that says whose it is, so no assertion is even expressible.

    This is the check the field makes possible, written out as a caller
    would: it holds whether or not the SQL that produced the results was
    correctly scoped.
    """
    principal = SessionUser(user_id="usr_mine")
    results = [
        RetrievedMemory(
            chunk_id=1,
            kind=MemoryKind.KB_ARTICLE,
            source_id="kb_daily_limits",
            content="KB text",
            similarity=0.9,
        ),
        RetrievedMemory(
            chunk_id=2,
            kind=MemoryKind.CALL_SUMMARY,
            source_id="sess_mine",
            content="my past call",
            similarity=0.8,
            user_id="usr_mine",
        ),
    ]

    assert all(
        r.kind is MemoryKind.KB_ARTICLE or r.user_id == principal.user_id for r in results
    )

    leaked = RetrievedMemory(
        chunk_id=3,
        kind=MemoryKind.CALL_SUMMARY,
        source_id="sess_theirs",
        content="another shop's history",
        similarity=0.95,
        user_id="usr_theirs",
    )

    assert not (leaked.kind is MemoryKind.KB_ARTICLE or leaked.user_id == principal.user_id)


def test_retrieved_memory_cannot_represent_an_ownerless_call_summary() -> None:
    """Mirrors `ck_memory_chunks_user_scope` on the read side, so a fake or
    hand-built result cannot describe an ownership state the stored row
    could never have been in."""
    with pytest.raises(ValidationError, match="must be set for kind='call_summary'"):
        RetrievedMemory(
            chunk_id=1,
            kind=MemoryKind.CALL_SUMMARY,
            source_id="sess_x",
            content="...",
            similarity=0.8,
        )


def test_retrieved_memory_rejects_a_kb_article_with_an_owner() -> None:
    with pytest.raises(ValidationError, match="NULL for kind='kb_article'"):
        RetrievedMemory(
            chunk_id=1,
            kind=MemoryKind.KB_ARTICLE,
            source_id="kb_daily_limits",
            content="...",
            similarity=0.8,
            user_id="usr_rajesh01",
        )


# --------------------------------------------------------------------------
# Blank-tenant states (representable-but-invalid, closed at the freeze)
# --------------------------------------------------------------------------


def test_session_user_rejects_a_blank_user_id() -> None:
    """`SessionUser` is the tenant boundary for both tool authorization and
    memory retrieval. app/api/deps.py already rejects a falsy JWT `sub`
    before building one, so this is unreachable today — but a defence that
    lives only in the caller is one refactor away from not existing."""
    with pytest.raises(ValidationError):
        SessionUser(user_id="")


def test_memory_chunk_rejects_a_blank_user_id() -> None:
    """`user_id=""` is not None, so it satisfies the kind/user
    biconditional while matching no principal — a call summary that looks
    scoped and is reachable by nobody. Paired with
    `ck_memory_chunks_user_id_not_blank` at the DB boundary."""
    with pytest.raises(ValidationError):
        MemoryChunk(
            kind=MemoryKind.CALL_SUMMARY,
            source_id="a1f3c9",
            content="...",
            embedding=_vector(),
            user_id="",
        )


def test_user_profile_rejects_a_blank_user_id() -> None:
    with pytest.raises(ValidationError):
        UserProfile(user_id="")


# --------------------------------------------------------------------------
# Size and cardinality bounds (docs/09 §5.2 caps keys, not values)
# --------------------------------------------------------------------------


def test_profile_facts_reject_an_oversized_value() -> None:
    """The closed schema bounds *keys*; nothing bounded values. These
    fields are merchant-influenced through voice -> STT -> Haiku
    extraction and render into the 200-token profile slot, so an
    unbounded `business_name` is both a slot-budget and a memory hazard."""
    with pytest.raises(ValidationError):
        UserProfileFacts(business_name="K" * 5000)


def test_profile_facts_accept_a_realistic_value() -> None:
    """The caps must not trim legitimate data — sized for headroom, not
    for a tight fit."""
    facts = UserProfileFacts(business_name="Kumar General Store", city="Jaipur")

    assert facts.business_name == "Kumar General Store"


def test_open_issues_cardinality_is_bounded() -> None:
    """The merge is append-shaped with no doc-specified cap, so an
    extractor opening an issue every call would crowd out the rest of the
    profile slot."""
    issue = OpenIssue(
        id="iss_001",
        summary="...",
        status="pending",
        opened_call="a1f3c9",
        opened_at=datetime(2026, 7, 24, tzinfo=UTC),
    )

    with pytest.raises(ValidationError):
        UserProfile(user_id="usr_rajesh01", open_issues=tuple(issue for _ in range(21)))


def test_memory_chunk_content_is_bounded() -> None:
    """~300-token KB chunks and ≤250-token summaries (docs/09 §6.1); the
    cap is ~6x headroom over both and exists to make a multi-megabyte blob
    unrepresentable."""
    with pytest.raises(ValidationError):
        MemoryChunk(
            kind=MemoryKind.KB_ARTICLE,
            source_id="kb_daily_limits",
            content="x" * 50_000,
            embedding=_vector(),
        )


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
