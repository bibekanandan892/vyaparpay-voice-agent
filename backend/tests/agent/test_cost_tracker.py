"""Unit tests for `CostTracker` (app/agent/cost_tracker.py) — pure
in-process: a recording `CostRepo` subclass replaces the Postgres write, a
duck-typed Redis double records `append_turn` calls, and span attributes
are asserted on a real SDK span (no global tracer provider is installed).

Price expectations use the config defaults (app/config.py, docs/05 §3.4's
table): dialogue $3/M in, $15/M out, $0.30/M cached-in; utility $1/M in,
$5/M out, $0.10/M cached-in.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any, cast

import pytest
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.trace import Span
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

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

# docs/16 §5's canonical-call Assumptions table, transcribed.
CANONICAL_CALL_SECONDS = 300.0  # 5-minute call
CANONICAL_DIALOGUE_INVOCATIONS = 20  # 15 turns + ~5 tool-loop second passes
CANONICAL_TTS_CHARS = 2_300  # agent speaks ~2.5 of the 5 minutes
CANONICAL_EMBEDDING_TOKENS = 2_000  # post-call summary + retrieval queries


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
    # cap. `settings.call_cost_cap_usd` happens to hold the same value,
    # but no production caller passes it: `ConversationManager` takes
    # this default, so the setting is inert (see its comment in
    # app/config.py). This asserts the two agree, not that the setting
    # is wired.
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
    # No media stage recorded usage on this LLM-only call, so its
    # components are zero — the "did not run" zero of judgment call #6,
    # asserted alongside the log fields that say so in
    # `test_finalize_names_the_stages_that_did_not_run`.
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


# ----------------------------------------------------------------------
# Non-LLM components — STT minutes, TTS characters, embedding tokens
# ----------------------------------------------------------------------


async def test_finalize_prices_stt_audio_per_minute_from_the_rounded_seconds(
    settings: Settings,
) -> None:
    """docs/16 §2/§5: Deepgram Nova-3 at $0.0077/min. The stored
    `stt_seconds` INT and the stored `stt_usd` must reproduce each other
    (tracker judgment call #8), so the fractional accumulator is rounded
    once and the price computed from the rounded value.

    The expected figures are written out rather than recomputed from
    `settings`: recomputing `seconds * rate / 60` here would just be
    `_stt_usd()`'s body transcribed, and would survive any rate change.
    """
    harness = _Harness(settings)
    harness.tracker.record_stt_audio(90.0)
    harness.tracker.record_stt_audio(0.4)

    await harness.tracker.finalize("sess_stt")

    row = harness.repo.upserts[0]
    assert row["stt_seconds"] == 90
    # 90 s = 1.5 min x $0.0077 = $0.01155 exactly.
    assert row["stt_usd"] == Decimal("0.011550")


@pytest.mark.parametrize(
    ("accumulated", "billed"),
    [
        (90.4, 90),  # below the half — every rounding mode agrees
        (90.5, 91),  # ON the half — only ROUND_HALF_UP/HALF_EVEN-odd give 91
        (90.6, 91),
    ],
)
async def test_stt_seconds_rounds_half_up(
    settings: Settings, accumulated: float, billed: int
) -> None:
    """Pins the rounding MODE, not just that rounding happens. The 90.5
    case is the one that discriminates: HALF_DOWN, HALF_EVEN, FLOOR and
    DOWN all give 90 there, so a fixture that only ever tests 90.4 lets
    the mode be changed silently."""
    harness = _Harness(settings)
    harness.tracker.record_stt_audio(accumulated)

    await harness.tracker.finalize(f"sess_round_{billed}")

    assert harness.repo.upserts[0]["stt_seconds"] == billed


async def test_finalize_prices_tts_characters_at_the_configured_rate(
    settings: Settings,
) -> None:
    """docs/16 §5's TTS line. The unit rate is config, derived from that
    section's own "~2,300 chars -> $0.15" pair (app/config.py states the
    derivation) — so 2,300 characters must land on $0.15 exactly."""
    harness = _Harness(settings)
    harness.tracker.record_tts_text(1_150)
    harness.tracker.record_tts_text(1_150)

    await harness.tracker.finalize("sess_tts")

    row = harness.repo.upserts[0]
    assert row["tts_chars"] == 2_300
    assert row["tts_usd"] == Decimal("0.150000")


async def test_finalize_prices_embedding_tokens_at_the_configured_rate(
    settings: Settings,
) -> None:
    """docs/16 §2: text-embedding-3-small at $0.02/M tokens. The column is
    NUMERIC(10,6) precisely so this line does not vanish (docs/12 §4.5)."""
    harness = _Harness(settings)
    harness.tracker.record_embedding_tokens(2_000)

    await harness.tracker.finalize("sess_emb")

    # 2,000 x $0.02/M = $0.00004 — non-zero at NUMERIC(10,6).
    assert harness.repo.upserts[0]["embeddings_usd"] == Decimal("0.000040")


@pytest.mark.parametrize(
    ("method", "value"),
    [("record_stt_audio", -0.5), ("record_tts_text", -1), ("record_embedding_tokens", -1)],
)
def test_record_methods_reject_negative_usage(
    settings: Settings, method: str, value: float
) -> None:
    """A negative quantity is a metering bug; it must not quietly refund
    the call's ledger."""
    tracker = _Harness(settings).tracker

    with pytest.raises(ValueError):
        getattr(tracker, method)(value)


async def _finalized_log(harness: _Harness, session_id: str) -> dict[str, Any]:
    with capture_logs() as logs:
        await harness.tracker.finalize(session_id)
    return next(entry for entry in logs if entry["event"] == "cost_tracker.finalized")


@pytest.mark.parametrize(
    ("stage", "record"),
    [
        ("llm", lambda t: t.record_turn(_DIALOGUE_USAGE, _DIALOGUE_MODEL, _start_span())),
        ("stt", lambda t: t.record_stt_audio(0.0)),
        ("tts", lambda t: t.record_tts_text(0)),
        ("embeddings", lambda t: t.record_embedding_tokens(0)),
    ],
)
async def test_each_stage_marks_itself_recorded_even_reporting_zero(
    settings: Settings, stage: str, record: Callable[[CostTracker], object]
) -> None:
    """Every one of the four `record_*` seams must set its own marker,
    and a ZERO report still counts as "this stage reported" — that is the
    distinction `unrecorded_stages` exists to draw (judgment call #6).
    Parametrized because a per-stage marker that was never wired up would
    otherwise hide behind whichever stage the test happened to use."""
    harness = _Harness(settings)
    record(harness.tracker)

    finalized = await _finalized_log(harness, f"sess_stage_{stage}")

    assert finalized["recorded_stages"] == [stage]
    assert stage not in finalized["unrecorded_stages"]
    assert sorted(finalized["unrecorded_stages"] + [stage]) == sorted(
        ["llm", "stt", "tts", "embeddings"]
    )


async def test_finalize_separates_a_stage_reporting_zero_from_one_reporting_nothing(
    settings: Settings,
) -> None:
    """The row cannot draw this distinction — `call_costs` has no
    stage-ran column, and both cases write $0. Note what the log does NOT
    claim: a stage that never ran and a stage whose metering is broken
    both simply never call their method, so they are indistinguishable
    here. The reader supplies that context (on a voice call, `stt`/`tts`
    in `unrecorded_stages` is a wiring alarm)."""
    harness = _Harness(settings)
    harness.tracker.record_turn(_DIALOGUE_USAGE, _DIALOGUE_MODEL, _start_span())
    harness.tracker.record_tts_text(0)  # ran, spoke nothing

    finalized = await _finalized_log(harness, "sess_stages")

    assert finalized["recorded_stages"] == ["llm", "tts"]
    assert finalized["unrecorded_stages"] == ["stt", "embeddings"]
    # Identical $0 in the row for the reported-zero and never-reported
    # stages; only the log fields above tell them apart.
    row = harness.repo.upserts[0]
    assert row["tts_usd"] == Decimal("0")
    assert row["stt_usd"] == Decimal("0")


# ----------------------------------------------------------------------
# The canonical call — docs/16 §5's budget, reconciled end to end
# ----------------------------------------------------------------------


def _record_canonical_call(tracker: CostTracker) -> None:
    """Replay docs/16 §5's canonical 5-minute call through the tracker,
    line for line from that section's Assumptions table:

    - 5 min / ~20 dialogue invocations at ~2,200 in (~1,400 of it cached
      prefix) / ~80 out;
    - utility: 2 Haiku folds at 1,000 in + 250 out each (docs/09 §4's
      stated fold shape) plus classification/compression, ~10k tokens
      across all three;
    - ~2,300 TTS characters (agent speaks ~2.5 of the 5 minutes);
    - ~2k embedding tokens (post-call summary + retrieval queries).
    """
    for _ in range(CANONICAL_DIALOGUE_INVOCATIONS):
        tracker.record_turn(
            {
                "prompt_tokens": 2_200,
                "completion_tokens": 80,
                "prompt_tokens_details": {"cached_tokens": 1_400},
            },
            _DIALOGUE_MODEL,
            _start_span(),
        )
    for _ in range(2):  # the two summary folds
        tracker.record_turn(
            {"prompt_tokens": 1_000, "completion_tokens": 250}, _UTILITY_MODEL, _start_span()
        )
    tracker.record_turn(  # classification + context compression
        {"prompt_tokens": 7_500, "completion_tokens": 0}, _UTILITY_MODEL, _start_span()
    )
    tracker.record_stt_audio(CANONICAL_CALL_SECONDS)
    tracker.record_tts_text(CANONICAL_TTS_CHARS)
    tracker.record_embedding_tokens(CANONICAL_EMBEDDING_TOKENS)


async def test_canonical_call_reconciles_against_the_docs16_cost_table(
    settings: Settings,
) -> None:
    """The self-reconciling budget test: replay docs/16 §5's canonical
    call and assert every component and the total.

    Each expected value is arithmetic over the config unit prices, so a
    price edit or a dropped line item fails here loudly instead of
    quietly redrawing the Grafana cost board.
    """
    harness = _Harness(settings)
    _record_canonical_call(harness.tracker)

    await harness.tracker.finalize("sess_canonical")
    row = harness.repo.upserts[0]

    # 300 s = 5 min x $0.0077/min.
    assert row["stt_usd"] == Decimal("0.038500")
    # 20 x [(2200-1400) x $3 + 1400 x $0.30 + 80 x $15] / 1M.
    assert row["llm_dialogue_usd"] == Decimal("0.080400")
    # 9,500 in x $1/M + 500 out x $5/M.
    assert row["llm_utility_usd"] == Decimal("0.012000")
    # 2,000 x $0.02/M.
    assert row["embeddings_usd"] == Decimal("0.000040")
    # 2,300 chars at the config rate derived from docs/16 §5's own pair.
    assert row["tts_usd"] == Decimal("0.150000")
    # coturn is self-hosted: docs/16 §5 budgets $0.00 marginal.
    assert row["turn_infra_usd"] == Decimal("0")
    assert row["total_usd"] == Decimal("0.280940")
    # The DB CHECK's exact equation (ck_call_costs_total_usd).
    assert row["total_usd"] == (
        row["stt_usd"]
        + row["llm_dialogue_usd"]
        + row["llm_utility_usd"]
        + row["embeddings_usd"]
        + row["tts_usd"]
        + row["turn_infra_usd"]
    )
    # Usage drivers, so the row stays auditable against invoices.
    assert row["stt_seconds"] == 300
    assert row["tts_chars"] == 2_300
    assert row["input_tokens"] == 53_500  # 20 x 2,200 dialogue + 9,500 utility
    assert row["cached_input_tokens"] == 28_000  # 20 x 1,400
    assert row["output_tokens"] == 2_100  # 20 x 80 dialogue + 500 utility


async def test_canonical_call_lands_within_a_cent_of_the_docs16_line_items(
    settings: Settings,
) -> None:
    """Same call, read against docs/16 §5's published per-component
    figures rather than against our own arithmetic.

    Every line matches at cent precision except LLM dialogue, which comes
    out one cent under the doc. That gap is exact and explained, not a
    tolerance: docs/16 §5's Assumptions table prices cache *writes* at
    1.25x input while `record_turn` has a single cached-token rate
    (app/config.py: reads at 0.1x). The missing premium is the canonical
    call's one 1,400-token prefix write:
    1,400 x $3 x (1.25 - 0.10) / 1M = $0.00483, which is what moves
    $0.0804 across the $0.085 rounding boundary to the doc's $0.09.
    Closing it needs `cache_creation_input_tokens` plumbed as a separate
    usage field; this test pins the gap so that change updates the doc
    reference too.
    """
    harness = _Harness(settings)
    _record_canonical_call(harness.tracker)

    await harness.tracker.finalize("sess_canonical_doc")
    row = harness.repo.upserts[0]

    def cents(value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.01"))

    assert cents(row["stt_usd"]) == Decimal("0.04")  # docs/16 §5: $0.04
    assert cents(row["llm_utility_usd"]) == Decimal("0.01")  # docs/16 §5: $0.01
    assert cents(row["tts_usd"]) == Decimal("0.15")  # docs/16 §5: $0.15
    # docs/16 §5 says "<$0.001". Asserted as an exact figure, not against
    # that bound: zero satisfies the bound, so `< 0.001` would pass with
    # the embeddings line deleted entirely.
    assert row["embeddings_usd"] == Decimal("0.000040")

    assert cents(row["llm_dialogue_usd"]) == Decimal("0.08")  # docs/16 §5 says $0.09
    # NOT a behavioural assertion — this expression reads only config
    # constants and never touches cost_tracker.py. It is the derivation
    # of the gap, written executably so the $0.00483 in the docstring
    # cannot drift from the rates it is derived from. The line below it
    # is the behavioural one: it reads the row.
    cache_write_premium = (
        Decimal("1400")
        * settings.llm_dialogue_input_usd_per_mtok
        * (Decimal("1.25") - Decimal("0.10"))
        / Decimal("1000000")
    )
    assert cache_write_premium == Decimal("0.00483")
    assert cents(row["llm_dialogue_usd"] + cache_write_premium) == Decimal("0.09")


async def test_canonical_call_makes_tts_the_largest_line_item(settings: Settings) -> None:
    """docs/16 §5's first stated observation: "TTS is the biggest line
    item (50% of the cached total), not the LLM." That is the claim the
    whole cost model turns on, so it is asserted rather than assumed —
    and it is exactly the claim a dropped `tts_usd` would break."""
    harness = _Harness(settings)
    _record_canonical_call(harness.tracker)

    await harness.tracker.finalize("sess_canonical_tts")
    row = harness.repo.upserts[0]

    # Exact component equality, not `max(...) == "tts_usd"` plus a >=50%
    # share: both of those survive a large TTS underprice (TTS stays the
    # largest line item until it is cut by roughly half), which is
    # exactly the drift this test is supposed to catch.
    assert {
        key: row[key]
        for key in (
            "stt_usd",
            "llm_dialogue_usd",
            "llm_utility_usd",
            "embeddings_usd",
            "tts_usd",
            "turn_infra_usd",
        )
    } == {
        "stt_usd": Decimal("0.038500"),
        "llm_dialogue_usd": Decimal("0.080400"),
        "llm_utility_usd": Decimal("0.012000"),
        "embeddings_usd": Decimal("0.000040"),
        "tts_usd": Decimal("0.150000"),
        "turn_infra_usd": Decimal("0"),
    }
    # docs/16 §5's observation, restated as the ordering it implies.
    assert row["tts_usd"] > row["llm_dialogue_usd"] + row["llm_utility_usd"]
    assert row["tts_usd"] / row["total_usd"] >= Decimal("0.50")


# ----------------------------------------------------------------------
# Budget guard — the consequence of the components becoming real
# ----------------------------------------------------------------------


def test_call_total_includes_every_component_not_just_llm_spend(
    settings: Settings,
) -> None:
    """Judgment call #7. docs/15 §5 calls the $1 cap "~3x the ≈ $0.30
    expected cost"; ≈ $0.30 is docs/16 §5's all-component figure, so the
    guard only holds that ratio if `call_total()` is all-component too."""
    tracker = _Harness(settings).tracker
    _record_canonical_call(tracker)

    # Full precision, unquantized: the TTS rate is stated to 6 dp, so
    # 2,300 chars price at $0.1499999993 rather than a flat $0.15. Only
    # the DB boundary rounds (the row reads $0.280940).
    assert tracker.call_total() == Decimal("0.2809399993")
    assert tracker.over_budget(cap_usd=Decimal("0.28")) is True
    assert tracker.over_budget(cap_usd=Decimal("0.29")) is False


def test_budget_guard_trips_on_runaway_media_spend_with_no_extra_llm_turns(
    settings: Settings,
) -> None:
    """The failure mode the cap exists for is not LLM-specific: a call
    held open streaming audio and speech burns provider money with a
    flat LLM bill. Before the media components were priced, `call_total()`
    could not see that at all."""
    tracker = _Harness(settings).tracker
    tracker.record_turn(_DIALOGUE_USAGE, _DIALOGUE_MODEL, _start_span())

    assert tracker.over_budget() is False
    tracker.record_stt_audio(3_600.0)  # a one-hour held-open line
    tracker.record_tts_text(15_000)
    assert tracker.over_budget() is True


# ----------------------------------------------------------------------
# take_unpublished_media_cost — the cross-process counter's media half
# ----------------------------------------------------------------------


def test_take_unpublished_media_cost_returns_the_delta_since_the_last_take(
    settings: Settings,
) -> None:
    """Judgment call #9. `ConversationManager` folds this into the
    `session:{id}.cost_usd` read-modify-write it already awaits per LLM
    round, so it must hand back only what is NEW — returning the running
    media total instead would compound the counter every turn."""
    tracker = _Harness(settings).tracker

    tracker.record_stt_audio(60.0)  # 1 min x $0.0077
    assert tracker.take_unpublished_media_cost() == Decimal("0.0077")

    tracker.record_tts_text(2_300)  # + $0.1499999993
    assert tracker.take_unpublished_media_cost() == Decimal("0.1499999993")


def test_take_unpublished_media_cost_is_zero_when_nothing_new_was_metered(
    settings: Settings,
) -> None:
    tracker = _Harness(settings).tracker
    tracker.record_tts_text(1_000)
    tracker.take_unpublished_media_cost()

    assert tracker.take_unpublished_media_cost() == Decimal("0")


def test_taking_the_media_delta_never_changes_the_authoritative_total(
    settings: Settings,
) -> None:
    """The watermark is publish-tracking only. `call_total()` and the
    finalized row must be identical whether or not anyone drained —
    otherwise the Redis guard rail would silently deduct from the
    ledger."""
    drained = _Harness(settings).tracker
    untouched = _Harness(settings).tracker
    for tracker in (drained, untouched):
        _record_canonical_call(tracker)
    drained.take_unpublished_media_cost()

    assert drained.call_total() == untouched.call_total()
    assert drained.take_unpublished_media_cost() == Decimal("0")


def test_summed_media_deltas_equal_the_finalized_media_components(
    settings: Settings,
) -> None:
    """What reaches Redis across a call must reconcile with what reaches
    Postgres, or the two per-call totals diverge silently — the exact
    failure this drain exists to prevent."""
    harness = _Harness(settings)
    published = Decimal("0")
    harness.tracker.record_stt_audio(120.0)
    published += harness.tracker.take_unpublished_media_cost()
    harness.tracker.record_tts_text(2_300)
    harness.tracker.record_embedding_tokens(2_000)
    published += harness.tracker.take_unpublished_media_cost()

    assert published == Decimal("0.0154") + Decimal("0.1499999993") + Decimal("0.00004")
    assert published == harness.tracker.call_total()  # no LLM turns recorded
