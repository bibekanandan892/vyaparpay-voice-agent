"""CostTracker — per-turn token/cost attribution, the running per-call
total, the $1 budget guard predicate, and the finalized `call_costs` row
(docs/05-agent-architecture.md §3.8).

One `CostTracker` instance per call/session: the in-memory running total
IS the per-call counter, so ConversationManager (a later task) constructs
one at session start and calls `finalize()` at hang-up.

Judgment calls, flagged per house style:

1. **The frozen sync `record_turn` cannot do Redis I/O** —
   `CostTrackerProto` pins it as `def`, not `async def`, while both Redis
   writes docs name for it are async: the `session:{id}.cost_usd` running
   counter (docs/05 §3.8) and the `session:{id}:turns` per-turn record
   (`RedisClient.append_turn`'s comment names CostTracker the appender).
   Resolution: `record_turn` stays pure (compute + span attrs + in-memory
   accumulation, deterministic and I/O-free); `finalize()` — the one
   async method the Proto grants — appends one Redis turn record per
   recorded turn before writing the Postgres row. A fire-and-forget task
   from inside `record_turn` was rejected: it needs a running loop,
   swallows failures unless babysat with done-callbacks (house rule:
   never silently swallow), and makes the method nondeterministic under
   test. Trade-off accepted: per-turn records reach Redis at hang-up, not
   mid-call; the running `cost_usd` counter bump is the caller's per-turn
   duty via the already-existing `SessionMemory.add_cost` (which
   ConversationManager awaits right after `record_turn` returns).
2. **`turn_no` in the Redis record is this tracker's 1-based record
   sequence**, not necessarily `conversation_turns.turn_no` — utility
   completions (summary folds) also record turns here. The post-call
   drain correlates by order/model; a records-real-turn-numbers upgrade
   needs a Proto change this task doesn't own.
3. **Missing usage fields -> `cost_estimated=True` with zero/partial
   figures, never a guess.** docs/05 §3.8's `chars/3.5` estimation clause
   needs the turn's text, which this frozen signature does not receive —
   the honest seam is to price what the provider reported (absent fields
   as 0) and mark the turn estimated, leaving the text-proxy fallback to
   the caller that has the text.
4. **Unknown model slugs price at dialogue rates, marked estimated.**
   Only the configured dialogue/utility models have pricing in Settings;
   a fallback-array model (docs/05 §3.4) only ever serves dialogue-tier
   requests, so dialogue rates are the best available proxy — and the
   `cost_estimated` flag keeps the dashboard honest about it.
5. **Span attribute keys are docs/04 §7.2's frozen `llm.total` names**
   (`input_tokens`, `output_tokens`, `cost_usd`, `model`) — docs/05
   §3.8's prose "`llm.input_tokens`" reads as "the `input_tokens`
   attribute on the llm span", per docs/04's span-attribute table, which
   is the one declared frozen by canon. `cost_usd` goes on the span as a
   float (`Decimal` is not an OTel AttributeValue); `Decimal` stays
   authoritative everywhere money is computed or persisted.
6. **Phase 2 is text-only**: `stt_usd`/`tts_usd`/`embeddings_usd` (and
   `stt_seconds`/`tts_chars`) finalize as zero — those pipelines don't
   exist until Phase 3/5. `total_usd` is computed as the sum of the
   already-quantized components so `ck_call_costs_total_usd`
   (app/models/orm.py) can never trip on rounding drift.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from opentelemetry.trace import Span
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.data.redis_client import RedisClient
from app.data.repositories.cost_repo import CostRepo
from app.domain.types import TurnCost
from app.obs.logging import get_logger
from app.obs.tracing import safe_set_attribute

log = get_logger(__name__)

_USD_PER_MILLION_TOKENS = Decimal("1000000")
# call_costs money columns are NUMERIC(10, 6) (app/models/orm.py) — this
# is the DB-boundary quantum. Per-turn TurnCost.cost_usd keeps full
# precision so the running total never accumulates rounding drift.
_MONEY_QUANTUM = Decimal("0.000001")
_ZERO_USD = Decimal("0")


class _ModelPricing:
    """Per-model USD-per-million-token prices, resolved once from
    Settings (frozen-by-convention value holder; plain class rather than
    a pydantic model — it never crosses a boundary)."""

    __slots__ = ("cached_input_usd_per_mtok", "input_usd_per_mtok", "output_usd_per_mtok")

    def __init__(
        self,
        *,
        input_usd_per_mtok: Decimal,
        cached_input_usd_per_mtok: Decimal,
        output_usd_per_mtok: Decimal,
    ) -> None:
        self.input_usd_per_mtok = input_usd_per_mtok
        self.cached_input_usd_per_mtok = cached_input_usd_per_mtok
        self.output_usd_per_mtok = output_usd_per_mtok


class CostTracker:
    """Implements `app.domain.interfaces.CostTrackerProto` (docs/05 §3.8).

    Constructor injection: `settings` (pricing + model slugs),
    `session_factory` (one short-lived AsyncSession per `finalize()` —
    finalize runs post-call, outside any request-scoped `get_db`
    session), `redis` (the `session:{id}:turns` appender), and
    `cost_repo_factory` as a testability seam defaulting to the real
    `CostRepo`.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        redis: RedisClient,
        cost_repo_factory: Callable[[AsyncSession], CostRepo] = CostRepo,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._redis = redis
        self._cost_repo_factory = cost_repo_factory
        self._turns: tuple[TurnCost, ...] = ()
        self._turn_records_flushed = False
        # Pricing is config, never constants in this logic (canon §5).
        self._pricing: dict[str, _ModelPricing] = {
            settings.openrouter_dialogue_model: _ModelPricing(
                input_usd_per_mtok=settings.llm_dialogue_input_usd_per_mtok,
                cached_input_usd_per_mtok=settings.llm_dialogue_cached_input_usd_per_mtok,
                output_usd_per_mtok=settings.llm_dialogue_output_usd_per_mtok,
            ),
            settings.openrouter_utility_model: _ModelPricing(
                input_usd_per_mtok=settings.llm_utility_input_usd_per_mtok,
                cached_input_usd_per_mtok=settings.llm_utility_cached_input_usd_per_mtok,
                output_usd_per_mtok=settings.llm_utility_output_usd_per_mtok,
            ),
        }

    # ------------------------------------------------------------------
    # Per-turn attribution (docs/05 §3.8 "Attribution")
    # ------------------------------------------------------------------

    def record_turn(self, usage: dict[str, Any], model: str, span: Span) -> TurnCost:
        """Price one completion's provider usage and attribute it: span
        attributes (docs/04 §7.2's `llm.total` keys, via the
        `safe_set_attribute` allowlist) plus the in-memory per-call
        accumulation. Pure/synchronous by contract — see module docstring
        judgment call #1 for where the Redis writes went."""
        input_tokens, output_tokens, cached_tokens, usage_complete = _parse_usage(usage)
        pricing = self._pricing.get(model)
        if pricing is None:
            # Judgment call #4: fallback-served slug — dialogue-rate proxy.
            log.warning("cost_tracker.unknown_model_priced_at_dialogue_rates", model=model)
            pricing = self._pricing[self._settings.openrouter_dialogue_model]
        cost_estimated = not usage_complete or model not in self._pricing

        uncached_input = max(input_tokens - cached_tokens, 0)
        cost_usd = (
            Decimal(uncached_input) * pricing.input_usd_per_mtok
            + Decimal(cached_tokens) * pricing.cached_input_usd_per_mtok
            + Decimal(output_tokens) * pricing.output_usd_per_mtok
        ) / _USD_PER_MILLION_TOKENS

        turn = TurnCost(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_tokens,
            cost_usd=cost_usd,
            cost_estimated=cost_estimated,
        )

        safe_set_attribute(span, "input_tokens", input_tokens)
        safe_set_attribute(span, "output_tokens", output_tokens)
        safe_set_attribute(span, "cost_usd", float(cost_usd))
        safe_set_attribute(span, "model", model)
        if cost_estimated:
            # docs/05 §3.8 failure behavior: only marked when estimated.
            safe_set_attribute(span, "cost_estimated", True)

        # Immutable accumulation: rebuild the tuple, never mutate a shared
        # list in place (house immutability rule).
        self._turns = (*self._turns, turn)
        return turn

    # ------------------------------------------------------------------
    # Running total + budget guard (docs/05 §3.8 "Budget guard")
    # ------------------------------------------------------------------

    def call_total(self) -> Decimal:
        return sum((turn.cost_usd for turn in self._turns), _ZERO_USD)

    def over_budget(self, cap_usd: Decimal = Decimal("1.00")) -> bool:
        """Strictly-over predicate (docs/05 §3.8: "On call_total() > $1").
        The default mirrors the frozen Proto; production callers pass
        `settings.call_cost_cap_usd` explicitly. Acting on a breach (log,
        span event, degrade signal, escalate offer) is the caller's
        orchestration, not this predicate's."""
        return self.call_total() > cap_usd

    # ------------------------------------------------------------------
    # Finalize (docs/05 §3.8 / docs/09 §8 — the once-per-call durable row)
    # ------------------------------------------------------------------

    async def finalize(self, session_id: str) -> None:
        """Write the per-call ledger: the deferred `session:{id}:turns`
        records (judgment call #1) and the `call_costs` upsert —
        idempotent on `session_id` (CostRepo.upsert's documented retry
        insurance); the turn-record append is guarded by a flushed flag
        so a finalize retry corrects the row without duplicating records.
        """
        if not self._turn_records_flushed:
            for turn_no, turn in enumerate(self._turns, start=1):
                await self._redis.append_turn(
                    session_id,
                    {
                        "turn_no": turn_no,  # tracker sequence — see judgment call #2
                        "model": turn.model,
                        "input_tokens": turn.input_tokens,
                        "cached_input_tokens": turn.cached_input_tokens,
                        "output_tokens": turn.output_tokens,
                        # str, not float: Decimal round-trips losslessly as
                        # text (same convention as RedisClient's cost field).
                        "cost_usd": str(turn.cost_usd),
                        "cost_estimated": turn.cost_estimated,
                    },
                )
            self._turn_records_flushed = True

        utility_model = self._settings.openrouter_utility_model
        llm_dialogue_usd = _quantize_usd(
            sum(
                (t.cost_usd for t in self._turns if t.model != utility_model),
                _ZERO_USD,
            )
        )
        llm_utility_usd = _quantize_usd(
            sum(
                (t.cost_usd for t in self._turns if t.model == utility_model),
                _ZERO_USD,
            )
        )
        # Judgment call #6: Phase 2 is text-only — non-LLM components are
        # zero, and total is the sum of the quantized components so the
        # ck_call_costs_total_usd CHECK holds by construction.
        total_usd = llm_dialogue_usd + llm_utility_usd

        async with self._session_factory() as db_session:
            repo = self._cost_repo_factory(db_session)
            await repo.upsert(
                session_id,
                stt_usd=_ZERO_USD,
                llm_dialogue_usd=llm_dialogue_usd,
                llm_utility_usd=llm_utility_usd,
                embeddings_usd=_ZERO_USD,
                tts_usd=_ZERO_USD,
                total_usd=total_usd,
                stt_seconds=0,
                input_tokens=sum(t.input_tokens for t in self._turns),
                cached_input_tokens=sum(t.cached_input_tokens for t in self._turns),
                output_tokens=sum(t.output_tokens for t in self._turns),
                tts_chars=0,
            )
            # Repos never commit (app/data/repositories/base.py: "that
            # boundary belongs to whatever opened the session") — finalize
            # opened this session, so finalize commits it.
            await db_session.commit()

        log.info(
            "cost_tracker.finalized",
            session_id=session_id,
            total_usd=str(total_usd),
            turns=len(self._turns),
        )


def _parse_usage(usage: dict[str, Any]) -> tuple[int, int, int, bool]:
    """Parse an OpenRouter/OpenAI-compatible usage frame tolerantly.

    Returns (input_tokens, output_tokens, cached_input_tokens, complete).
    `complete` is False when either headline count is absent (judgment
    call #3). Cached-prefix tokens surface either as a top-level
    `cached_tokens` (the shape `UsageEvent`'s docstring shows) or as
    OpenAI's nested `prompt_tokens_details.cached_tokens`; both are
    accepted, and absence of the cached detail alone does NOT mark the
    turn estimated — plenty of models simply have no cache hit to report.
    Cached tokens are a subset of `prompt_tokens` (the OpenAI convention),
    so the uncached share is `prompt - cached`, floored at zero.
    """
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    complete = prompt_tokens is not None and completion_tokens is not None

    details = usage.get("prompt_tokens_details") or {}
    cached_tokens = usage.get("cached_tokens")
    if cached_tokens is None:
        cached_tokens = details.get("cached_tokens")

    return (
        int(prompt_tokens or 0),
        int(completion_tokens or 0),
        int(cached_tokens or 0),
        complete,
    )


def _quantize_usd(value: Decimal) -> Decimal:
    """Round to `call_costs`' NUMERIC(10, 6) scale at the DB boundary
    only — see `_MONEY_QUANTUM`'s comment."""
    return value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)


__all__ = ["CostTracker"]
