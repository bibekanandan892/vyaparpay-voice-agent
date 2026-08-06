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
6. **Non-LLM spend is metered by the stages that produce it, not
   inferred here.** STT is billed per audio-minute, TTS per character and
   embeddings per token — none of which is derivable from an LLM usage
   frame. `record_stt_audio` / `record_tts_text` /
   `record_embedding_tokens` are therefore the seams the producing stages
   call; this class only prices and totals. `finalize()` logs
   `recorded_stages`/`unrecorded_stages`, which separates "the stage
   reported zero" from "the stage reported nothing at all" — NOT "did
   not run" from "wiring broke", which are the same observation from
   here (a stage that never ran and a stage whose wiring is broken both
   simply never call the method). What makes the field useful is
   context the reader supplies: on a voice call, `stt`/`tts` appearing
   in `unrecorded_stages` is a wiring alarm, because those stages
   certainly ran. `total_usd` is computed as the sum of the
   already-quantized components so `ck_call_costs_total_usd`
   (app/models/orm.py) can never trip on rounding drift.
7. **`call_total()` includes every component, not just LLM spend.** The
   $1 default cap is docs/15 §6's "~3x the ≈ $0.30 expected cost"
   runaway backstop, and ≈ $0.30 is docs/16 §5's *all-component* figure —
   so the guard only means what that doc says when the total it reads is
   the all-component one. Against an LLM-only total (this class before
   B7, ≈ $0.09 of a canonical call) the same $1 cap admits several
   dollars of real spend before it fires.

   docs/15 §6 does not name `call_total()` though: its cost-cap row
   specifies the watchdog against "running `cost_usd` in the
   `session:{id}` hash" — a Redis field, readable cross-process, which
   `call_total()` is not (this instance lives in the voice worker; only
   the worker process can see it). Leaving that field LLM-only would
   reintroduce the exact understatement B7 exists to fix, in the
   component that terminates calls, so `take_unpublished_media_cost()`
   (judgment call #9) feeds media into it too.
9. **Media spend reaches Redis on a per-turn watermark, not per
   chunk.** `record_stt_audio`/`record_tts_text` are sync callbacks on
   the audio pump and the sentence dispatcher — tens of calls per second
   — and `SessionMemory.add_cost` is an async read-modify-write. Doing
   that I/O per chunk is out on the hot path, and firing it forget-style
   from a sync callback is what judgment call #1 already rejected.
   Instead `take_unpublished_media_cost()` returns everything metered
   since the previous call and moves a watermark; `ConversationManager`
   folds it into the `add_cost` it *already* awaits per LLM round, so
   the Redis counter converges with no extra round trip.

   The honest cost of that choice: the Redis counter trails
   `call_total()` by whatever media spend has accrued since the last LLM
   round — in practice the current turn's own TTS, since that is spoken
   after the round that generated it. Media spend after the final LLM
   round never reaches Redis at all. `call_costs` (written by
   `finalize()` from the in-memory accumulators) is the authoritative
   ledger; the Redis field is a cross-process guard rail that is close,
   not equal.
8. **`stt_seconds` is rounded once, and the price is computed from the
   rounded value.** `call_costs.stt_seconds` is `INT` (docs/12 §4.5) and
   the row is documented as auditable against provider invoices, so the
   stored driver and the stored cost must reproduce each other exactly;
   pricing the unrounded accumulator would leave them up to half a
   second apart.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final

from opentelemetry.trace import Span
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.data.redis_client import RedisClient
from app.data.repositories.cost_repo import CostRepo
from app.domain.types import TurnCost
from app.obs.logging import get_logger
from app.obs.tracing import safe_set_attribute

log = get_logger(__name__)

# Shared by the per-million-TOKEN (LLM, embeddings) and per-million-
# CHARACTER (TTS) rates — both are quoted per million in Settings.
_PER_MILLION = Decimal("1000000")
_SECONDS_PER_MINUTE = Decimal("60")
# call_costs money columns are NUMERIC(10, 6) (app/models/orm.py) — this
# is the DB-boundary quantum. Per-turn TurnCost.cost_usd keeps full
# precision so the running total never accumulates rounding drift.
_MONEY_QUANTUM = Decimal("0.000001")
_WHOLE = Decimal("1")
_ZERO_USD = Decimal("0")

# Judgment call #6's stage names. These are log/diagnostic labels for
# "did this priced stage report any usage on this call", not schema:
# `call_costs` has no stage-ran column.
_STAGE_LLM: Final = "llm"
_STAGE_STT: Final = "stt"
_STAGE_TTS: Final = "tts"
_STAGE_EMBEDDINGS: Final = "embeddings"
_PRICED_STAGES: Final = (_STAGE_LLM, _STAGE_STT, _STAGE_TTS, _STAGE_EMBEDDINGS)


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
        # Judgment call #6: non-LLM usage drivers, accumulated by the
        # stages that produce them.
        self._stt_seconds: Decimal = Decimal("0")
        self._tts_chars = 0
        self._embedding_tokens = 0
        self._stages_recorded: frozenset[str] = frozenset()
        # Judgment call #9: how much media spend has already been handed
        # to a caller for publishing to the Redis running counter.
        self._published_media_usd: Decimal = Decimal("0")
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
        ) / _PER_MILLION

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
        self._stages_recorded |= {_STAGE_LLM}
        return turn

    # ------------------------------------------------------------------
    # Non-LLM usage (judgment call #6) — metered by the producing stage
    # ------------------------------------------------------------------

    def record_stt_audio(self, seconds: float) -> None:
        """Add audio handed to the STT provider, in seconds.

        Deepgram streaming is billed per minute of submitted audio, so the
        quantity this expects is the duration of the PCM actually sent on
        the STT socket — not the wall-clock call length, which also counts
        time before the first offer and after the last frame. Calling this
        with 0.0 still marks the STT stage as having run.
        """
        if seconds < 0:
            raise ValueError(f"stt audio seconds must be non-negative, got {seconds}")
        # via str() so the accumulator holds the decimal value the caller
        # meant rather than a float's binary expansion. Hygiene, not
        # behaviour: a mutation to `Decimal(seconds)` survives the suite,
        # because at call-length scales the difference is ~1e-22 s and
        # cannot move `_billed_stt_seconds()`. Deliberately not pinned by
        # a test — there is no behaviour to pin.
        self._stt_seconds += Decimal(str(seconds))
        self._stages_recorded |= {_STAGE_STT}

    def record_tts_text(self, characters: int) -> None:
        """Add characters submitted to the TTS provider.

        ElevenLabs bills on submitted characters, so this is counted at
        dispatch: a sentence whose stream dies part-way was still billed.

        **This figure can differ from the invoice in either direction**,
        because this class cannot see how far the provider actually got.
        Three known sources, none measurable from here:

        - *Under*: the stream-input protocol sends two space-bearing
          frames per sentence — the `{"text": " "}` begin-of-stream frame
          and the sentence itself as `"<text> "`
          (app/providers/elevenlabs.py judgment call 2, its lines 208-209)
          — so two characters per sentence go uncounted.
        - *Under*: the before-first-byte fallback-voice retry (judgment
          call 6 there) resubmits a whole sentence invisibly to callers.
        - *Over*: `ElevenLabsTts.synthesize` is an async **generator**
          function, so calling it opens no connection; the socket is
          dialled on the first `anext()`, inside `SpeechDispatcher`'s
          `async for`. Characters are already counted by then. If the
          vendor is unreachable, every sentence fails at connect and this
          ledger records characters the vendor never received — a whole
          call's worth, ~$0.15 on the docs/16 §5 canonical call, on a row
          documented as auditable against invoices.

        Dispatch-time counting is still the right default: the common
        failure is a stream dying mid-sentence, which *was* billed. But a
        row is a best-effort reconstruction of the invoice, not the
        invoice.
        """
        if characters < 0:
            raise ValueError(f"tts characters must be non-negative, got {characters}")
        self._tts_chars += characters
        self._stages_recorded |= {_STAGE_TTS}

    def record_embedding_tokens(self, tokens: int) -> None:
        """Add tokens billed by the embeddings provider.

        No caller exists yet: `OpenAIEmbeddings` is not constructed in any
        composition root (`app/voice/run.py`, `app/api/main.py`,
        `scripts/demo_cli.py`), so the embeddings stage does not run on
        any live path and `embeddings_usd` is legitimately zero. This
        method is the seam the wiring task calls; until then the stage
        shows up in `finalize()`'s `unrecorded_stages`.
        """
        if tokens < 0:
            raise ValueError(f"embedding tokens must be non-negative, got {tokens}")
        self._embedding_tokens += tokens
        self._stages_recorded |= {_STAGE_EMBEDDINGS}

    # ------------------------------------------------------------------
    # Pricing of the non-LLM components
    # ------------------------------------------------------------------

    def _billed_stt_seconds(self) -> int:
        """Judgment call #8: the one rounding, shared by the priced
        figure and the `stt_seconds` column."""
        return int(self._stt_seconds.quantize(_WHOLE, rounding=ROUND_HALF_UP))

    def _stt_usd(self) -> Decimal:
        rate = self._settings.stt_usd_per_audio_minute
        return Decimal(self._billed_stt_seconds()) * rate / _SECONDS_PER_MINUTE

    def _tts_usd(self) -> Decimal:
        return Decimal(self._tts_chars) * self._settings.tts_usd_per_mchar / _PER_MILLION

    def _embeddings_usd(self) -> Decimal:
        rate = self._settings.embeddings_usd_per_mtok
        return Decimal(self._embedding_tokens) * rate / _PER_MILLION

    def _media_usd(self) -> Decimal:
        """Everything `call_total()` adds beyond the LLM turns."""
        return self._stt_usd() + self._tts_usd() + self._embeddings_usd()

    def take_unpublished_media_cost(self) -> Decimal:
        """Media spend metered since the previous call, and move the
        watermark (judgment call #9).

        The caller is expected to be publishing to the cross-process
        `session:{id}.cost_usd` counter docs/15 §6's cost-cap watchdog is
        specified against, which `record_turn`'s LLM figure alone would
        leave ~3x low. Sync and free of awaits, so two callers in the same
        event loop cannot both take the same delta.

        Not idempotent by design: a caller whose Redis write then fails
        has already consumed the delta and will under-report it. That is
        the same failure mode `SessionMemory.add_cost` already has for LLM
        cost (its read-modify-write is unlocked and unretried), and the
        authoritative ledger is `call_costs`, not this counter.
        """
        total = self._media_usd()
        delta = total - self._published_media_usd
        self._published_media_usd = total
        return delta

    # ------------------------------------------------------------------
    # Running total + budget guard (docs/05 §3.8 "Budget guard")
    # ------------------------------------------------------------------

    def call_total(self) -> Decimal:
        """Every priced component, per judgment call #7 — not LLM spend
        alone."""
        return sum((turn.cost_usd for turn in self._turns), _ZERO_USD) + self._media_usd()

    def over_budget(self, cap_usd: Decimal = Decimal("1.00")) -> bool:
        """Strictly-over predicate (docs/05 §3.8: "On call_total() > $1").
        The default mirrors the frozen Proto and equals the
        `settings.call_cost_cap_usd` default; the one production caller
        (`ConversationManager._run_turn`) takes that default rather than
        passing the setting, so overriding `CALL_COST_CAP_USD` in env has
        no effect today. Acting on a breach (log, span event, degrade
        signal, escalate offer) is the caller's orchestration, not this
        predicate's."""
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
                        # Code-review CRITICAL, fixed: every record_turn()
                        # call prices an LLM completion the agent produced
                        # — always the agent's turn. SessionManager.end()'s
                        # drain reads this key unconditionally
                        # (docs/12 §4.2's role CHECK requires it); omitting
                        # it raised KeyError on every real call's drain.
                        "role": "agent",
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
        stt_usd = _quantize_usd(self._stt_usd())
        tts_usd = _quantize_usd(self._tts_usd())
        embeddings_usd = _quantize_usd(self._embeddings_usd())
        # coturn is self-hosted, so docs/16 §5 budgets its marginal
        # per-call cost at $0.00; passed explicitly rather than left to
        # the column default so the CHECK's six terms are all visible here.
        turn_infra_usd = _ZERO_USD
        # Total is the sum of the QUANTIZED components so the
        # ck_call_costs_total_usd CHECK holds by construction.
        total_usd = (
            stt_usd
            + llm_dialogue_usd
            + llm_utility_usd
            + embeddings_usd
            + tts_usd
            + turn_infra_usd
        )

        async with self._session_factory() as db_session:
            repo = self._cost_repo_factory(db_session)
            await repo.upsert(
                session_id,
                stt_usd=stt_usd,
                llm_dialogue_usd=llm_dialogue_usd,
                llm_utility_usd=llm_utility_usd,
                embeddings_usd=embeddings_usd,
                tts_usd=tts_usd,
                turn_infra_usd=turn_infra_usd,
                total_usd=total_usd,
                stt_seconds=self._billed_stt_seconds(),
                input_tokens=sum(t.input_tokens for t in self._turns),
                cached_input_tokens=sum(t.cached_input_tokens for t in self._turns),
                output_tokens=sum(t.output_tokens for t in self._turns),
                tts_chars=self._tts_chars,
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
            stt_usd=str(stt_usd),
            llm_dialogue_usd=str(llm_dialogue_usd),
            llm_utility_usd=str(llm_utility_usd),
            embeddings_usd=str(embeddings_usd),
            tts_usd=str(tts_usd),
            # Judgment call #6: the row alone cannot say whether a $0
            # component means "that stage did not run on this call" or
            # "that stage ran and its metering is broken". These two
            # fields are where that distinction is recorded.
            recorded_stages=sorted(self._stages_recorded),
            unrecorded_stages=[s for s in _PRICED_STAGES if s not in self._stages_recorded],
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
