"""Unit tests for `CostTracker` (app/agent/cost_tracker.py) — pure
in-process: a recording `CostRepo` subclass replaces the Postgres write, a
duck-typed Redis double records `append_turn` calls, and span attributes
are asserted on a real SDK span (no global tracer provider is installed).

Price expectations use the config defaults (app/config.py, docs/05 §3.4's
table): dialogue $3/M in, $15/M out, $0.30/M cached-in; utility $1/M in,
$5/M out, $0.10/M cached-in.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

import pytest
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.trace import Span
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.cost_tracker import CostTracker
from app.config import Settings
from app.data.redis_client import RedisClient
from app.data.repositories.cost_repo import CostRepo

_DIALOGUE_MODEL = "anthropic/claude-sonnet-5"  # config default, pinned by conftest settings
_UTILITY_MODEL = "anthropic/claude-haiku-4-5"

# 1000 prompt (400 of them cached) + 100 completion at dialogue rates:
# (600 * $3 + 400 * $0.30 + 100 * $15) / 1M = $0.00342 exactly.
_DIALOGUE_USAGE = {
    "prompt_tokens": 1000,
    "completion_tokens": 100,
    "prompt_tokens_details": {"cached_tokens": 400},
}
_DIALOGUE_COST = Decimal("0.00342")

# 2000 prompt + 200 completion, no cache, at utility rates:
# (2000 * $1 + 200 * $5) / 1M = $0.003 exactly.
_UTILITY_USAGE = {"prompt_tokens": 2000, "completion_tokens": 200}
_UTILITY_COST = Decimal("0.003")


# ----------------------------------------------------------------------
# Doubles
# ----------------------------------------------------------------------


class _RecordingCostRepo(CostRepo):
    """Records `upsert` kwargs instead of executing SQL — the unit seam
    for asserting exactly what finalize() writes."""

    def __init__(self) -> None:  # deliberately no AsyncSession
        self.upserts: list[dict[str, Any]] = []

    async def upsert(
        self,
        session_id: str,
        *,
        stt_usd: Decimal,
        llm_dialogue_usd: Decimal,
        llm_utility_usd: Decimal,
        embeddings_usd: Decimal,
        tts_usd: Decimal,
        total_usd: Decimal,
        stt_seconds: int,
        input_tokens: int,
        cached_input_tokens: int,
        output_tokens: int,
        tts_chars: int,
        turn_infra_usd: Decimal = Decimal("0"),
    ) -> None:
        self.upserts.append(
            {
                "session_id": session_id,
                "stt_usd": stt_usd,
                "llm_dialogue_usd": llm_dialogue_usd,
                "llm_utility_usd": llm_utility_usd,
                "embeddings_usd": embeddings_usd,
                "tts_usd": tts_usd,
                "turn_infra_usd": turn_infra_usd,
                "total_usd": total_usd,
                "stt_seconds": stt_seconds,
                "input_tokens": input_tokens,
                "cached_input_tokens": cached_input_tokens,
                "output_tokens": output_tokens,
                "tts_chars": tts_chars,
            }
        )


class _FakeRedis:
    def __init__(self) -> None:
        self.turns: list[tuple[str, dict[str, Any]]] = []

    async def append_turn(self, session_id: str, turn: dict[str, Any]) -> None:
        self.turns.append((session_id, turn))


class _FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


class _FakeSessionFactory:
    """Stands in for `async_sessionmaker`: calling it yields an async
    context manager over one fake session."""

    def __init__(self) -> None:
        self.session = _FakeSession()

    def __call__(self) -> _FakeSessionFactory:
        return self

    async def __aenter__(self) -> _FakeSession:
        return self.session

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _Harness:
    """One CostTracker wired to recording doubles."""

    def __init__(self, settings: Settings) -> None:
        self.repo = _RecordingCostRepo()
        self.redis = _FakeRedis()
        self.session_factory = _FakeSessionFactory()
        self.tracker = CostTracker(
            settings,
            session_factory=cast(
                "async_sessionmaker[AsyncSession]", self.session_factory
            ),
            redis=cast(RedisClient, self.redis),
            cost_repo_factory=lambda _db: self.repo,
        )


def _start_span() -> Span:
    """A real SDK span from a throwaway (never globally installed)
    provider — its recorded attributes are directly assertable."""
    return TracerProvider().get_tracer(__name__).start_span("llm.total")


def _attributes(span: Span) -> dict[str, Any]:
    return dict(cast(ReadableSpan, span).attributes or {})


# ----------------------------------------------------------------------
# record_turn — Decimal cost math + usage parsing
# ----------------------------------------------------------------------


def test_record_turn_computes_exact_decimal_cost_at_dialogue_rates(
    settings: Settings,
) -> None:
    tracker = _Harness(settings).tracker

    turn = tracker.record_turn(_DIALOGUE_USAGE, _DIALOGUE_MODEL, _start_span())

    assert turn.cost_usd == _DIALOGUE_COST
    assert isinstance(turn.cost_usd, Decimal)
    assert turn.input_tokens == 1000
    assert turn.output_tokens == 100
    assert turn.cached_input_tokens == 400
    assert turn.model == _DIALOGUE_MODEL
    assert turn.cost_estimated is False


def test_record_turn_prices_utility_model_at_utility_rates(settings: Settings) -> None:
    tracker = _Harness(settings).tracker

    turn = tracker.record_turn(_UTILITY_USAGE, _UTILITY_MODEL, _start_span())

    assert turn.cost_usd == _UTILITY_COST
    assert turn.cached_input_tokens == 0
    assert turn.cost_estimated is False


def test_record_turn_accepts_top_level_cached_tokens_variant(settings: Settings) -> None:
    """OpenRouter surfaces cached counts either nested (OpenAI's
    `prompt_tokens_details.cached_tokens`) or top-level (`cached_tokens`,
    the shape UsageEvent's docstring shows) — both must price the same."""
    tracker = _Harness(settings).tracker
    usage = {"prompt_tokens": 1000, "completion_tokens": 100, "cached_tokens": 400}

    turn = tracker.record_turn(usage, _DIALOGUE_MODEL, _start_span())

    assert turn.cost_usd == _DIALOGUE_COST
    assert turn.cached_input_tokens == 400


def test_record_turn_missing_usage_fields_marks_estimated_with_zero_figures(
    settings: Settings,
) -> None:
    """No chars/3.5 guessing at this seam (module docstring judgment call
    #3): absent headline counts -> zero figures, flagged estimated."""
    tracker = _Harness(settings).tracker
    span = _start_span()

    turn = tracker.record_turn({}, _DIALOGUE_MODEL, span)

    assert turn.cost_usd == Decimal("0")
    assert turn.input_tokens == 0
    assert turn.output_tokens == 0
    assert turn.cost_estimated is True
    assert _attributes(span)["cost_estimated"] is True


def test_record_turn_unknown_model_prices_at_dialogue_rates_and_marks_estimated(
    settings: Settings,
) -> None:
    """A fallback-array model (docs/05 §3.4) has no configured pricing —
    dialogue rates are the proxy, and the estimate is flagged honestly."""
    tracker = _Harness(settings).tracker

    turn = tracker.record_turn(_DIALOGUE_USAGE, "openai/gpt-5.1", _start_span())

    assert turn.cost_usd == _DIALOGUE_COST  # same tokens, dialogue rates
    assert turn.model == "openai/gpt-5.1"
    assert turn.cost_estimated is True


def test_record_turn_sets_canonical_span_attributes(settings: Settings) -> None:
    """docs/04 §7.2's frozen llm.total attribute names, through the
    safe_set_attribute allowlist — cost as float (spans), Decimal stays
    the money type everywhere else."""
    tracker = _Harness(settings).tracker
    span = _start_span()

    tracker.record_turn(_DIALOGUE_USAGE, _DIALOGUE_MODEL, span)

    attributes = _attributes(span)
    assert attributes["input_tokens"] == 1000
    assert attributes["output_tokens"] == 100
    assert attributes["cost_usd"] == pytest.approx(0.00342)
    assert attributes["model"] == _DIALOGUE_MODEL
    assert "cost_estimated" not in attributes  # only set when estimated


# ----------------------------------------------------------------------
# call_total / over_budget
# ----------------------------------------------------------------------


def test_call_total_accumulates_across_turns_as_exact_decimal(
    settings: Settings,
) -> None:
    tracker = _Harness(settings).tracker
    tracker.record_turn(_DIALOGUE_USAGE, _DIALOGUE_MODEL, _start_span())
    tracker.record_turn(_UTILITY_USAGE, _UTILITY_MODEL, _start_span())

    assert tracker.call_total() == _DIALOGUE_COST + _UTILITY_COST


def test_over_budget_is_false_at_exactly_the_cap(settings: Settings) -> None:
    """docs/05 §3.8: the guard fires on call_total() > cap — strictly
    over, so a total sitting exactly at the cap is still in budget."""
    tracker = _Harness(settings).tracker
    tracker.record_turn(_DIALOGUE_USAGE, _DIALOGUE_MODEL, _start_span())

    assert tracker.over_budget(cap_usd=_DIALOGUE_COST) is False
    assert tracker.over_budget(cap_usd=_DIALOGUE_COST - Decimal("0.000001")) is True


def test_over_budget_default_cap_matches_the_dollar_runaway_guard(
    settings: Settings,
) -> None:
    tracker = _Harness(settings).tracker
    # 400k uncached prompt tokens at $3/M = $1.20 — past the $1 default
    # cap (which equals settings.call_cost_cap_usd, the value production
    # callers pass explicitly).
    tracker.record_turn(
        {"prompt_tokens": 400_000, "completion_tokens": 0}, _DIALOGUE_MODEL, _start_span()
    )

    assert tracker.over_budget() is True
    assert tracker.over_budget(cap_usd=settings.call_cost_cap_usd) is True


def test_over_budget_is_false_with_no_recorded_turns(settings: Settings) -> None:
    tracker = _Harness(settings).tracker

    assert tracker.call_total() == Decimal("0")
    assert tracker.over_budget() is False


# ----------------------------------------------------------------------
# finalize — the call_costs upsert + deferred Redis turn records
# ----------------------------------------------------------------------


async def test_finalize_upserts_row_with_component_sum_total(
    settings: Settings,
) -> None:
    harness = _Harness(settings)
    harness.tracker.record_turn(_DIALOGUE_USAGE, _DIALOGUE_MODEL, _start_span())
    harness.tracker.record_turn(_UTILITY_USAGE, _UTILITY_MODEL, _start_span())

    await harness.tracker.finalize("sess_test_1")

    assert len(harness.repo.upserts) == 1
    row = harness.repo.upserts[0]
    assert row["session_id"] == "sess_test_1"
    # Phase 2 is text-only: every non-LLM component is zero (docstring
    # judgment call #6).
    assert row["stt_usd"] == Decimal("0")
    assert row["tts_usd"] == Decimal("0")
    assert row["embeddings_usd"] == Decimal("0")
    assert row["stt_seconds"] == 0
    assert row["tts_chars"] == 0
    # LLM components split by served model tier, quantized to NUMERIC(10,6).
    assert row["llm_dialogue_usd"] == Decimal("0.003420")
    assert row["llm_utility_usd"] == Decimal("0.003000")
    # The DB CHECK's exact equation (ck_call_costs_total_usd).
    assert row["total_usd"] == (
        row["stt_usd"]
        + row["llm_dialogue_usd"]
        + row["llm_utility_usd"]
        + row["embeddings_usd"]
        + row["tts_usd"]
        + row["turn_infra_usd"]
    )
    # Token aggregates across both turns.
    assert row["input_tokens"] == 3000
    assert row["cached_input_tokens"] == 400
    assert row["output_tokens"] == 300
    # finalize owns its session lifecycle, so it commits (base.py's
    # "commit belongs to whatever opened the session").
    assert harness.session_factory.session.commits == 1


async def test_finalize_buckets_fallback_served_models_as_dialogue_spend(
    settings: Settings,
) -> None:
    harness = _Harness(settings)
    harness.tracker.record_turn(_DIALOGUE_USAGE, "openai/gpt-5.1", _start_span())

    await harness.tracker.finalize("sess_test_2")

    row = harness.repo.upserts[0]
    assert row["llm_dialogue_usd"] == Decimal("0.003420")
    assert row["llm_utility_usd"] == Decimal("0")


async def test_finalize_appends_one_redis_turn_record_per_recorded_turn(
    settings: Settings,
) -> None:
    harness = _Harness(settings)
    harness.tracker.record_turn(_DIALOGUE_USAGE, _DIALOGUE_MODEL, _start_span())
    harness.tracker.record_turn(_UTILITY_USAGE, _UTILITY_MODEL, _start_span())

    await harness.tracker.finalize("sess_test_3")

    assert [(sid, t["turn_no"], t["model"]) for sid, t in harness.redis.turns] == [
        ("sess_test_3", 1, _DIALOGUE_MODEL),
        ("sess_test_3", 2, _UTILITY_MODEL),
    ]
    first_record = harness.redis.turns[0][1]
    assert first_record["input_tokens"] == 1000
    assert first_record["cached_input_tokens"] == 400
    assert first_record["output_tokens"] == 100
    assert first_record["cost_usd"] == str(_DIALOGUE_COST)  # lossless Decimal-as-text
    assert first_record["cost_estimated"] is False
    # Code-review CRITICAL, fixed: every drained record must carry
    # "role" — SessionManager.end()'s drain (task 4.1) reads it
    # unconditionally (docs/12 §4.2's role CHECK requires it) and every
    # record_turn() call prices an LLM completion, always the agent's.
    assert first_record["role"] == "agent"
    assert harness.redis.turns[1][1]["role"] == "agent"


async def test_finalize_retry_re_upserts_but_never_duplicates_turn_records(
    settings: Settings,
) -> None:
    """finalize is idempotent-on-retry (CostRepo.upsert corrects the row);
    the Redis appends are guarded so a retry doesn't double the ledger."""
    harness = _Harness(settings)
    harness.tracker.record_turn(_DIALOGUE_USAGE, _DIALOGUE_MODEL, _start_span())

    await harness.tracker.finalize("sess_test_4")
    await harness.tracker.finalize("sess_test_4")

    assert len(harness.repo.upserts) == 2  # upsert is the idempotent half
    assert len(harness.redis.turns) == 1  # append is the guarded half
