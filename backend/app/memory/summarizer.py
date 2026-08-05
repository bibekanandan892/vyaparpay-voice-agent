"""`Summarizer` — memory layer 3, the rolling conversation summary
(docs/09-memory-architecture.md §4). The one LLM-backed compression in the
system, and the only one that runs **off** the turn path.

The algorithm (docs/09 §4.1) is small and exact, and lives in this module
as four module-level pure functions so it can be tested without a model,
a Redis, or an event loop:

- While `turn_count <= 8` no summary exists; the window renders every turn
  verbatim.
- At turn 9 the first fold covers turns `1..3` — that is `1..N-6`, keeping
  the last 6 turns verbatim.
- Thereafter every 6 turns (15, 21, ...) as an incremental fold: input is
  the previous summary plus the turns now leaving the window, output
  covers `1..N-6` in <=250 tokens.
- `summary_thru_turn` records the boundary, so the window renderer knows
  to start at `thru + 1` and coverage never overlaps or gaps.

Judgment calls, flagged per house style:

1. **`kick()` is deliberately `def`, not `async def`.** docs/09 §4.1 rule
   4 requires the fold to run off the turn path: a ~1.5 s Haiku call
   inside a turn whose p95 budget is 2.0 s end to end would consume most
   of the turn on its own. A synchronous entry point cannot `await` the
   fold, so `kick()` itself cannot block a turn — that much is
   structural, and `tests/memory/test_summarizer.py` asserts the
   signature stays synchronous.

   **The scope of that guarantee, stated exactly: it covers `kick()`, not
   this module.** Two other public entry points here DO await the full
   ~1.5 s fold, and a turn-path caller reaching for either puts it back
   on the hot path without touching a signature:

   - `fold_pending()` is `async` because the post-call pipeline has to be
     able to await it.
   - `drain()` is `async` and waits for the in-flight fold to finish. Its
     docstring markets it for the post-call pipeline and for tests, but
     nothing stops `kick(...); await drain()` — the natural "make sure it
     landed" reflex — from being written in a turn loop, where it costs
     the whole fold.

   Nothing here prevents either; the off-path property belongs to *using
   `kick()` alone*, and any wiring review has to check which of the three
   a turn-path caller reached for.

   This is the fire-and-forget shape `app/agent/cost_tracker.py`'s
   judgment call #1 explicitly rejected, so its three objections are
   answered rather than ignored: *needs a running loop* — true, and
   `kick()` says so and lets the task factory's own `RuntimeError` fire
   loudly rather than catching it; *swallows failures* — `_guarded_fold`
   wraps the whole fold in an in-coroutine `try/except` (not an
   `add_done_callback`; there is none in this module) that logs every
   failure and records it on `last_error`, so nothing is silently
   dropped; *nondeterministic under test* — the task factory is injected
   and `drain()` awaits the in-flight fold, so tests never race. The
   difference in requirements is what forces the different answer:
   nothing asked CostTracker's per-turn record to be off-path, and
   docs/09 §4.1 asks this of the fold in as many words.

2. **This module buffers the turns it will fold, in its own per-call
   state.** It cannot read them back from `SessionMemory`: that window is
   capped at the last 8 *messages* (`_MAX_TRANSCRIPT_TURNS`) while docs/09
   §4's algorithm counts *turns*, and a turn is two messages at minimum.
   By the time the fold at N=15 needs turns 4-9, those messages left the
   window several turns earlier. The buffer is bounded (see
   `_MAX_BUFFERED_TURNS`) and sheds every turn a fold successfully
   covered.

3. **A failed fold keeps its input.** Buffered turns are dropped only
   after the write to the store succeeds, so a fold that errors or times
   out is not a hole — the next fold point simply folds a wider range
   (`prev_thru+1 .. N-6`). `summary_thru_turn` never advances past what
   was actually written, which is what keeps the window/summary boundary
   honest through a failure.

4. **An over-budget summary is logged, never truncated here.** docs/09 §4
   caps the summary at 250 tokens and the docs/11 §6 prompt asks for
   under 200 words; when a model overshoots anyway, cutting the text at a
   character offset is the one edit most likely to slice a rupee amount or
   a reference id in half — precisely the corruption docs/09 §4.2 exists
   to prevent. Mechanical slot-budget trimming is `ContextCompressor`'s
   job at render time (docs/09 §7 strategy 2). This module measures, puts
   the number on the span, warns, and stores the whole text.

5. **The token figure is the `chars / 3.5` proxy**, not a tokenizer
   count (`app/context/token_estimate.py`). It over-estimates, which is
   the safe direction for a budget warning, but it means the
   `summary_tokens` span attribute is an estimate and the over-budget
   warning can fire on a summary a real tokenizer would call compliant.

6. **The fold's TTFT deadline is not the turn path's.** `LLMRouter`
   defaults to 1.5 s and retries once — correct when a merchant is
   waiting on first audio, wrong here, where nobody is waiting and a
   spurious retry just doubles the fold's cost. `_FOLD_TTFT_DEADLINE_S`
   is deliberately generous; `_FOLD_TIMEOUT_S` is the real bound, because
   a fold that hangs forever would block every later fold behind the
   single-flight guard.

7. **This departs from docs/05 §3.9's pinned signature, in three
   ways.** docs/05 §3.9 is not silent about the shape — it pins one:

       class Summarizer:
           async def maybe_fold(self, session: Session) -> None: ...
           async def final_summary(self, session: Session) -> ConversationSummary: ...

   and docs/05's component table places `Summarizer` under `app/agent/`.
   This module matches none of the three, on purpose:

   - **`kick(session_id, turn_count)` is sync, not `async def
     maybe_fold(session)`.** `app/domain/types.py`'s `Session` carries
     `session_id`, `user_id`, `state`, `started_at`, `ended_at` — no
     `turn_count`. `maybe_fold(session)` therefore cannot decide whether
     this turn is a fold point at all without a second source, so it is
     not implementable as written. Independently, `async def` is the one
     shape judgment call #1 rules out: it can be `await`ed on the turn
     path, which docs/09 §4.1 rule 4 forbids.
   - **No `final_summary` counterpart here.** The durable half of memory
     layer 3 landed beside this as `ConversationSummaryStore` (same
     batch), but that class *persists* a `ConversationSummary` it is
     handed — nothing yet *generates* one, because resolution
     classification and intent extraction are post-call pipeline
     decisions with no implementation. `fold_pending(..., thru_turn=N)`
     is the text half of that seam and is documented as such below.
   - **`app/memory/`, not `app/agent/`.** docs/09 §1's layer table names
     this memory layer 3 and pairs it with `ConversationSummaryStore`;
     the two ship together, so they live together. The table cell being
     departed from marks its own row "§3.9 references" and points at
     docs/09 as the owner of this algorithm.

   Also no `Summarizer` Protocol in `app/domain/interfaces.py` — and the
   reason is narrower than "there is no signature to freeze", since
   §3.9's block above is one. The immediately preceding commit froze
   `SemanticMemoryProto` there with zero implementations precisely
   because docs/05 §3.7 pins *its* signature and batches were to build
   against it in parallel. The difference is that this module does not
   implement §3.9's shape: freezing the doc's shape would freeze
   something nothing implements, and freezing *this* shape would pre-empt
   the wiring task that will have the first real call site. If a Protocol
   is wanted, that task is where it belongs — and per `interfaces.py`'s
   own module docstring, a deviation from a pinned signature is a
   contract change that should update the doc-facing contract first.

Deliberately NOT in this module: rendering the summary into
`ContextBundle.memory_summary` (`ContextBuilder`'s slot, a later task),
the post-call `memory_chunks` embed, and resolution classification. The
seam for the post-call pipeline is `fold_pending(..., thru_turn=N)` — the
same fold with the boundary moved to the end of the call.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Callable, Coroutine, Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, cast

from opentelemetry.trace import Span, Status, StatusCode

from app.agent.cost_tracker import CostTracker
from app.context.token_estimate import estimate_tokens
from app.domain.interfaces import LLMRouterProto
from app.domain.types import (
    LLMEvent,
    Message,
    Role,
    RollingSummary,
    TaskKind,
    TokenEvent,
    UsageEvent,
)
from app.memory.session_memory import SessionMemory
from app.obs.logging import get_logger
from app.obs.tracing import SPAN_SUMMARY_FOLD, get_tracer, safe_set_attribute

log = get_logger(__name__)
tracer = get_tracer(__name__)

# docs/09 §4.1, rules 1-3. Named rather than inlined because every one of
# them appears in more than one arithmetic expression below, and an
# off-by-one between them is exactly the overlap/gap bug the boundary
# bookkeeping exists to prevent.
#
# FIRST_FOLD_TURN: the turn at which `turn_count` first exceeds the
#   8-turn verbatim allowance, so the first fold fires.
# WINDOW_TURNS: how many turns stay verbatim behind the boundary. The
#   summary covers `1..N-WINDOW_TURNS`; the window renders
#   `N-WINDOW_TURNS+1..N`.
# FOLD_INTERVAL: the re-fire cadence after the first fold.
FIRST_FOLD_TURN: Final = 9
WINDOW_TURNS: Final = 6
FOLD_INTERVAL: Final = 6

# docs/09 §4.2 / docs/11 §1: the `<memory_summary>` slot's budget. A
# breach is warned about, never truncated here — module docstring #4.
SUMMARY_TOKEN_BUDGET: Final = 250

# Judgment call #6: nobody is waiting on this stream, so the turn path's
# aggressive first-token deadline buys nothing and its one retry would
# double the fold's cost. The wall-clock timeout below is the real bound.
_FOLD_TTFT_DEADLINE_S: Final = 15.0
_FOLD_TIMEOUT_S: Final = 45.0

# Steady state buffers WINDOW_TURNS + FOLD_INTERVAL = 12 turns (the
# verbatim window plus the interval accumulating toward the next fold).
# The headroom above that is for consecutive fold failures, which retain
# their input (judgment call #3): this covers six in a row on top of a
# call far longer than docs/09 §4's 15-turn canonical one.
_MAX_BUFFERED_TURNS: Final = 48

_ZERO_USD: Final = Decimal("0")

# Judgment call: duplicated from `app/agent/context_builder.py`'s
# `_PROMPTS_DIR`/`_load_prompt` rather than imported — those are private
# to that module, and this task is forbidden from editing it. Both are
# one-line path constants over the same directory (docs/11 §7: prompts
# live under `backend/app/agent/prompts/*.md`). Consolidating the two
# into a shared prompt loader is a reasonable follow-up; doing it here
# would mean editing ContextBuilder.
_PROMPTS_DIR: Final = Path(__file__).resolve().parents[1] / "agent" / "prompts"
_ROLLING_SUMMARY_PROMPT: Final = "rolling_summary.md"

# Rendered when no fold has landed yet. The docs/11 §6 prompt interpolates
# `{summary}` unconditionally, and handing a model a bare empty line reads
# as a truncated prompt rather than as "there is nothing yet".
_NO_PRIOR_SUMMARY: Final = "(none — this is the first summary of this call)"

# How a buffered turn renders into the prompt's `{turns}` block. Role
# labels are the merchant-facing vocabulary ("customer"/"agent"), not the
# chat-array role names, because the prompt is written in those terms.
_ROLE_LABELS: Final[dict[Role, str]] = {
    Role.USER: "customer",
    Role.ASSISTANT: "agent",
    Role.SYSTEM: "system",
    Role.TOOL: "tool",
}


# --------------------------------------------------------------------------
# The algorithm (docs/09 §4.1), as pure functions
# --------------------------------------------------------------------------


def should_fold(turn_count: int) -> bool:
    """docs/09 §4.1 rules 1-3: fire at turn 9, then every 6 turns (15,
    21, ...). False for every other turn, including all of 1-8, where the
    window still holds the whole call verbatim."""
    if turn_count < FIRST_FOLD_TURN:
        return False
    return (turn_count - FIRST_FOLD_TURN) % FOLD_INTERVAL == 0


def summary_thru_turn(turn_count: int) -> int:
    """The coverage boundary a fold at `turn_count` writes: the summary
    covers turns `1..turn_count-6`. At N=9 that is 3, at N=15 it is 9 —
    docs/09 §4.1's worked examples."""
    return turn_count - WINDOW_TURNS


def window_start_turn(thru_turn: int) -> int:
    """The first turn the verbatim window renders, given a summary that
    covers `1..thru_turn`.

    This is the whole of the no-overlap/no-gap property, and it is one
    line: the window starts at exactly the turn after the boundary. Every
    turn `<= thru_turn` is in the summary and nowhere else; every turn
    `> thru_turn` is verbatim and nowhere else. `thru_turn = 0` (no
    summary yet) yields 1 — the window starts at the first turn, which is
    docs/09 §4.1 rule 1.

    Nothing in this module *enforces* that the renderer honors this — the
    renderer is `ContextBuilder`, which this task does not touch. This
    function is the shared definition both sides must agree on, exported
    so the wiring task has something to call instead of re-deriving
    `N - 6` by hand.
    """
    return thru_turn + 1


@dataclass(frozen=True)
class BufferedTurn:
    """One conversation turn held for a future fold: its 1-based turn
    number and the chat messages it produced (user utterance, assistant
    reply, and any tool messages between them).

    Frozen, like every value this module holds — the buffer is rebuilt as
    a new tuple on every change rather than mutated in place.
    """

    turn_no: int
    messages: tuple[Message, ...]


class Summarizer:
    """The rolling fold (docs/09 §4). **One instance per call**, like
    `CostTracker` — it holds this call's pending-turn buffer, its current
    summary, and its fold count.

    Constructor injection: `router` (utility tier — Haiku, docs/05 §3.4),
    `session_memory` (the `session:{id}` hash this writes the summary
    into), `cost_tracker` (a fold is ~$0.0023 and must land in the call's
    cost, docs/09 §4.1), and `task_factory` as the scheduling seam.

    **`task_factory` is production wiring, not only a test seam — and the
    default escapes docs/05 §4's cancellation boundary.** docs/05 §4:
    "the task group is the cancellation boundary... no turn task outlives
    the call that spawned it." The default, `asyncio.create_task`, spawns
    OUTSIDE any enclosing `TaskGroup`: verified by probe, a task so
    created is still alive when the group exits and runs to completion
    afterwards, on both clean and crashing teardown. So with the default,
    a fold in flight at hang-up orphans — it goes on writing the summary
    field of an ended call, and calls `cost_tracker.record_turn` after
    `finalize()` may already have written the `call_costs` row, so that
    fold's spend lands nowhere.

    **Whoever wires this must therefore do one of two things**, and
    `VoiceAgentWorker` already holds what the first needs (`self._tg`,
    which is how it schedules turns — `app/voice/worker.py`):

    - pass the call's `TaskGroup.create_task` as `task_factory`, which
      buys both halves of §4's contract: a clean hang-up waits for the
      fold, a crash cancels it; or
    - `await drain()` at teardown, which waits for the in-flight fold
      before the call's state goes away.

    Tests override it for a third reason — so the off-turn-path behavior
    is observable rather than raced against.
    """

    def __init__(
        self,
        router: LLMRouterProto,
        session_memory: SessionMemory,
        *,
        cost_tracker: CostTracker,
        task_factory: Callable[[Coroutine[Any, Any, Any]], asyncio.Task[Any]] = (
            asyncio.create_task
        ),
    ) -> None:
        self._router = router
        self._session_memory = session_memory
        self._cost_tracker = cost_tracker
        self._task_factory = task_factory
        self._prompt = (_PROMPTS_DIR / _ROLLING_SUMMARY_PROMPT).read_text(
            encoding="utf-8"
        ).strip()
        self._buffer: tuple[BufferedTurn, ...] = ()
        self._summary: RollingSummary | None = None
        self._fold_count = 0
        self._in_flight: asyncio.Task[Any] | None = None
        self._last_error: BaseException | None = None
        self._dropped_turns = 0
        self._rewound_turns = 0
        # Bridges the synchronous `_consume` (which sees the usage frame)
        # to `fold_pending`'s async `add_cost` — see `_consume`.
        self._pending_cost_usd: Decimal = _ZERO_USD

    # ------------------------------------------------------------------
    # Read-only state (the seam a caller/test inspects)
    # ------------------------------------------------------------------

    @property
    def summary(self) -> RollingSummary | None:
        """The current summary, or `None` before the first fold lands.

        This instance is the only writer to this session's summary during
        the call (docs/02: the worker that owns a call is the sole writer
        to its `session:{id}`), so this and the hash agree for as long as
        the instance lives.

        One divergence is possible and is NOT handled here: docs/09 §11's
        eviction row rebuilds an evicted session **in place** — "audio and
        tools keep working" — rather than dropping the call. This module
        has no eviction detection and never re-reads the hash, so if a
        rebuild reuses this instance, `_summary` stays populated while the
        hash is empty, and the stale text is what the next fold folds
        into. Making that correct needs the rebuild path to construct a
        fresh `Summarizer` (or an explicit reset seam); neither exists
        yet, because the rebuild path itself does not.
        """
        return self._summary

    @property
    def fold_count(self) -> int:
        """**Completed** folds this call — docs/09 §11's drift budget.
        Incremented only after the store write lands (judgment call #3).

        Not the same number as the `fold_no` span attribute, which is
        `fold_count + 1` stamped at *attempt* time: two consecutive failed
        folds both emit `fold_no=1` and neither becomes a `fold_count`.
        The two agree only on the success path. `fold_no` counts attempts
        at that boundary, this counts folds that actually wrote."""
        return self._fold_count

    @property
    def last_error(self) -> BaseException | None:
        """The most recent fold failure, retained so a failure that
        happened on a detached task is inspectable rather than only
        visible in the log (judgment call #1)."""
        return self._last_error

    @property
    def dropped_turns(self) -> int:
        """Turns evicted from the buffer by `_MAX_BUFFERED_TURNS`. Nonzero
        means a later summary has a content hole — see `observe_turn`."""
        return self._dropped_turns

    @property
    def rewound_turns(self) -> int:
        """Buffered turns replaced because `observe_turn` was called with
        a turn number that did not increase — a retried turn, or the
        docs/09 §11 eviction rebuild restarting `turn_count` mid-call.
        Kept distinct from `dropped_turns`: that one means the buffer
        overflowed and a summary loses content it should have had, this
        one means the caller's turn numbering moved backwards. See
        `observe_turn` for why neither raises."""
        return self._rewound_turns

    @property
    def pending_turns(self) -> tuple[BufferedTurn, ...]:
        return self._buffer

    # ------------------------------------------------------------------
    # Turn path: cheap, synchronous, no I/O
    # ------------------------------------------------------------------

    def observe_turn(self, turn_no: int, messages: Sequence[Message]) -> None:
        """Buffer one completed turn's messages for a future fold
        (judgment call #2). Pure in-memory bookkeeping — this runs on the
        turn path, so it does no I/O and calls no model.

        **Neither failure class on this path raises**, and that is one
        policy, not two. `observe_turn` runs inside a live voice turn, so
        raising here drops a merchant's call. Nothing this method can
        detect is worth that: the whole subsystem is a compression
        nicety, and docs/09 §11's degradation for losing it outright is
        "let me double-check that" plus a tool call, not a dropped call.

        **Buffer overflow drops the oldest turn.** The cost is stated
        rather than hidden — after a drop, the next fold's summary has a
        *content* hole for the dropped turns, while `summary_thru_turn`
        still advances to `N-6`. Rendering stays overlap-free and
        gap-free; the summary's coverage of that range is what degrades.
        `dropped_turns` and an ERROR log make it visible.

        **A turn number that does not increase replaces the buffered
        turns at or after it** (latest wins) rather than raising, which is
        a change of policy from this method's first version. Three pieces
        of evidence, in order of weight:

        1. *The eviction rebuild reaches this inside a live turn.* docs/09
           §11's eviction row rebuilds a session **in place** — "audio and
           tools keep working" — and explicitly restarts `turn_count`. If
           that rebuild reuses this instance (nothing constructs a fresh
           one; see the `summary` property), the first `observe_turn(1,
           ...)` afterwards is a regression, and raising would turn a
           documented, survivable degradation into a dropped call.
        2. *`kick()` already tolerates the same scenario.* Its
           already-covered arm names "a retried turn, or a caller that
           kicks more than once for turn N" and calls itself idempotent.
           A retried turn being idempotent at `kick()` and fatal here was
           one code path with two policies and only one of them argued.
        3. *Replace, not ignore.* Ignoring the incoming turn would keep a
           superseded first attempt over its retry, and after a rebuild
           would discard every turn for the rest of the call (each new
           low number losing to the stale high-water mark) — the
           summarizer would go blind. Replacing keeps the strictly
           increasing invariant true by construction instead of asserting
           it, and after a rebuild the discarded turns are the ones whose
           numbering the rebuild already invalidated; docs/09 §11 counts
           them as lost either way.

        What this does NOT put at risk: the coverage boundary. That comes
        from `kick()`'s `turn_count` / `fold_pending`'s `thru_turn`, never
        from the buffer, so a replacement changes what the fold *reads*,
        never what it *claims to cover*. `rewound_turns` and a WARNING log
        make it visible.
        """
        kept = tuple(t for t in self._buffer if t.turn_no < turn_no)
        rewound = len(self._buffer) - len(kept)
        if rewound:
            self._rewound_turns += rewound
            log.warning(
                "summarizer.turn_number_did_not_increase_replaced_buffered_turns",
                turn_no=turn_no,
                replaced=rewound,
                total_rewound=self._rewound_turns,
                previous_last_turn=self._buffer[-1].turn_no,
            )
        appended = (*kept, BufferedTurn(turn_no=turn_no, messages=tuple(messages)))
        if len(appended) > _MAX_BUFFERED_TURNS:
            overflow = len(appended) - _MAX_BUFFERED_TURNS
            self._dropped_turns += overflow
            log.error(
                "summarizer.buffer_overflow_dropped_oldest_turns",
                dropped=overflow,
                total_dropped=self._dropped_turns,
                limit=_MAX_BUFFERED_TURNS,
                oldest_kept_turn=appended[overflow].turn_no,
            )
            appended = appended[overflow:]
        self._buffer = appended

    def kick(self, session_id: str, turn_count: int) -> bool:
        """Schedule a fold if turn `turn_count` is a fold point, and
        return immediately. Called after turn N's TTS dispatch (docs/09
        §4.1 rule 4).

        Synchronous on purpose — see judgment call #1. Returns True when a
        fold was actually scheduled, False when there was nothing to do
        (not a fold turn, already covered, or one already in flight);
        every False arm logs its reason.

        Raises `RuntimeError` (from `asyncio.create_task`) if called with
        no running event loop. Not caught: the intended caller is the turn
        loop, which always has one. There is no such caller yet — this
        method is unwired (module docstring #7) — so "always has one" is a
        property of the intended wiring, not an observed fact about the
        current codebase. Swallowing the error would turn that wiring bug
        into a call that silently never summarizes.
        """
        if not should_fold(turn_count):
            return False
        thru = summary_thru_turn(turn_count)
        covered = self._summary.thru_turn if self._summary is not None else 0
        if thru <= covered:
            # Re-kick for a boundary already written (a retried turn, or a
            # caller that kicks more than once for turn N). Idempotent.
            log.info(
                "summarizer.fold_skipped_boundary_already_covered",
                session_id=session_id,
                turn_count=turn_count,
                thru_turn=thru,
                covered_thru_turn=covered,
            )
            return False
        if self._in_flight is not None and not self._in_flight.done():
            # Single-flight: two concurrent folds would race on the
            # summary field and could write the OLDER boundary last.
            # docs/09 §4.1's ~20 s/turn cadence against a ~1.5 s fold
            # means this should not happen; if it does, the fold is
            # degraded (a wider range next time), never corrupted.
            log.warning(
                "summarizer.fold_skipped_previous_still_running",
                session_id=session_id,
                turn_count=turn_count,
                thru_turn=thru,
            )
            return False
        self._in_flight = self._task_factory(self._guarded_fold(session_id, thru_turn=thru))
        return True

    async def drain(self) -> None:
        """Wait for an in-flight fold to finish, if any. Never raises —
        a fold failure is already logged and on `last_error`.

        For the post-call pipeline (which must not start reading the
        summary while a fold is still writing it), for call teardown when
        the default `task_factory` is in use (see the class docstring: it
        is the alternative to passing a `TaskGroup.create_task`, and
        without one of the two an in-flight fold orphans at hang-up), and
        for tests, which use it instead of sleeping.

        **Not for the turn path.** This awaits the whole ~1.5 s fold, so
        `kick(...); await drain()` in a turn loop undoes judgment call
        #1's entire point.
        """
        task = self._in_flight
        if task is None or task.done():
            return
        await asyncio.wait({task})

    # ------------------------------------------------------------------
    # The fold itself (docs/09 §4.1 rule 3, §4.2's contract)
    # ------------------------------------------------------------------

    async def fold_pending(self, session_id: str, *, thru_turn: int) -> RollingSummary | None:
        """Fold every buffered turn up to `thru_turn` into the summary and
        write it to the session hash. Returns the new summary, or `None`
        when there was nothing to fold.

        Awaited inline — this is the seam for callers that legitimately
        wait for the result: the post-call pipeline (docs/09 §8), which
        passes `thru_turn=<final turn count>` to fold the last verbatim
        turns in as well. The turn path must use `kick()` instead.

        Raises whatever the model call raises. `kick()`'s scheduled path
        wraps this in `_guarded_fold`, which logs and records instead.
        """
        departing = tuple(t for t in self._buffer if t.turn_no <= thru_turn)
        if not departing:
            log.info(
                "summarizer.fold_skipped_no_turns_in_range",
                session_id=session_id,
                thru_turn=thru_turn,
                buffered=len(self._buffer),
            )
            return None

        with tracer.start_as_current_span(
            # record_exception/set_status_on_exception off for the same
            # reason llm_router.py documents: the prompt this span wraps
            # carries transcript text with rupee amounts and reference
            # ids, and the default SDK behavior would attach an
            # exception's message and traceback to an exported span.
            SPAN_SUMMARY_FOLD,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            safe_set_attribute(span, "session_id", session_id)
            safe_set_attribute(span, "fold_no", self._fold_count + 1)
            safe_set_attribute(span, "summary_thru_turn", thru_turn)
            self._pending_cost_usd = _ZERO_USD
            try:
                text = await self._complete(departing, span)
            except BaseException as exc:
                span.set_status(Status(StatusCode.ERROR, description=type(exc).__name__))
                raise
            finally:
                # The model was billed whether or not the stream finished
                # cleanly, so the running counter is bumped on both paths.
                # `record_turn` (CostTracker's own accumulation, and thus
                # the `call_costs` row) already happened inside `_consume`;
                # this is only the Redis budget-guard counter.
                if self._pending_cost_usd:
                    await self._session_memory.add_cost(session_id, self._pending_cost_usd)
                    self._pending_cost_usd = _ZERO_USD

            tokens = estimate_tokens(text)
            safe_set_attribute(span, "summary_tokens", tokens)
            if tokens > SUMMARY_TOKEN_BUDGET:
                # Judgment call #4: warn, never truncate.
                log.warning(
                    "summarizer.summary_over_token_budget",
                    session_id=session_id,
                    thru_turn=thru_turn,
                    estimated_tokens=tokens,
                    budget=SUMMARY_TOKEN_BUDGET,
                )

        summary = RollingSummary(text=text, thru_turn=thru_turn)
        await self._session_memory.set_summary(session_id, summary)
        # Judgment call #3: state advances only after the write lands, so
        # a failure above leaves the previous boundary and the previous
        # buffer intact for a wider fold next time.
        self._summary = summary
        self._buffer = tuple(t for t in self._buffer if t.turn_no > thru_turn)
        self._fold_count += 1
        log.info(
            "summarizer.folded",
            session_id=session_id,
            fold_no=self._fold_count,
            thru_turn=thru_turn,
            turns_folded=len(departing),
            estimated_tokens=estimate_tokens(text),
        )
        return summary

    async def _guarded_fold(self, session_id: str, *, thru_turn: int) -> None:
        """`fold_pending` with its failures caught, for the detached task
        `kick()` schedules. Nothing awaits that task's result, so an
        uncaught exception there would surface only as asyncio's
        "exception was never retrieved" warning at GC time — this is the
        done-callback discipline `cost_tracker.py`'s judgment call #1 asks
        for, applied rather than argued with."""
        try:
            async with asyncio.timeout(_FOLD_TIMEOUT_S):
                await self.fold_pending(session_id, thru_turn=thru_turn)
        except asyncio.CancelledError:
            # Cancellation is expected, not an error — but be exact about
            # what actually reaches here, both verified by probe:
            #
            # - NOT `_FOLD_TIMEOUT_S` expiry. `asyncio.timeout` absorbs the
            #   CancelledError it raises and re-raises `TimeoutError`, which
            #   is not a CancelledError subclass, so a timed-out fold lands
            #   in the generic arm below and is logged as a failure.
            # - NOT call teardown, under the DEFAULT `task_factory`: that
            #   spawns outside any enclosing TaskGroup, so group teardown
            #   never cancels this task (see the class docstring). Under the
            #   default the fold orphans and keeps running instead.
            # - It DOES fire when something cancels the task explicitly:
            #   event-loop shutdown, or a caller who injected a
            #   TaskGroup-bound `task_factory` whose group is tearing down.
            #   That injection is the wiring the class docstring asks for,
            #   so this arm is what makes that wiring quiet rather than
            #   noisy — it is anticipatory today, not dead.
            log.info(
                "summarizer.fold_cancelled", session_id=session_id, thru_turn=thru_turn
            )
            raise
        except BaseException as exc:
            self._last_error = exc
            log.error(
                "summarizer.fold_failed",
                session_id=session_id,
                thru_turn=thru_turn,
                error_type=type(exc).__name__,
            )

    async def _complete(self, departing: Sequence[BufferedTurn], span: Span) -> str:
        """One utility-tier completion: render the docs/11 §6 prompt, run
        it, attribute its cost onto `span`, and return the summary text.

        Streams because that is the only shape `LLMRouterProto` offers;
        the content is accumulated and returned whole, since nothing
        downstream of a fold consumes it incrementally.
        """
        prompt = self._render_prompt(departing)
        opened = self._router.stream(
            [Message(role=Role.USER, content=prompt)],
            tier=self._router.route(TaskKind.UTILITY),
            ttft_deadline_s=_FOLD_TTFT_DEADLINE_S,
        )
        # `LLMRouterProto.stream` is spelled `async def ... -> AsyncIterator`,
        # which admits both an async generator (LLMRouter itself, called
        # un-awaited) and a coroutine returning one. Same reconciliation
        # `LLMRouter._open_stream` documents, for the same reason.
        resolved: Any = opened
        if inspect.iscoroutine(resolved):
            resolved = await resolved
        upstream = aiter(cast("AsyncIterator[LLMEvent]", resolved))

        parts: list[str] = []
        # An in-frame try/finally, not a bare `async for`. LLMRouter's
        # module docstring makes closing from the consuming frame a
        # REQUIREMENT of every `stream()` consumer, not a nicety: a loop
        # body that raises without closing leaves the router's `llm.total`
        # span to asyncio's async-generator finalizer, which runs on a
        # later tick with a different contextvars Context, and the span
        # then silently never exports.
        #
        # `contextlib.aclosing` would be the idiomatic spelling, but
        # `LLMRouterProto.stream` is typed as returning an
        # `AsyncIterator`, which carries no `aclose` — so the close is
        # conditional, matching `LLMRouter._close_quietly`'s own getattr
        # against the same Protocol.
        try:
            async for event in upstream:
                self._consume(event, parts, span)
        finally:
            aclose = getattr(upstream, "aclose", None)
            if aclose is not None:
                await aclose()
        text = "".join(parts).strip()
        if not text:
            # A fold that produced nothing must not overwrite a good
            # summary with an empty string — that would silently erase
            # every earlier turn's compressed history.
            raise ValueError(
                "rolling-summary fold returned no content — refusing to overwrite "
                "the existing summary with an empty string"
            )
        return text

    def _consume(self, event: LLMEvent, parts: list[str], span: Span) -> None:
        """Accumulate content, and attribute the fold's cost when the
        usage frame arrives (docs/09 §4.1: ~$0.0023 per fold).

        The `record_turn` + `add_cost` pairing mirrors
        `ConversationManager._consume_event`: the running
        `session:{id}.cost_usd` counter must include folds too, or a
        15-turn call's reported cost is short by two folds. The Redis bump
        is fired here rather than deferred because, unlike CostTracker's
        own per-turn records, this code is already off the turn path.

        `ToolCallsEvent` is not handled: the fold prompt sends no tools,
        so the router has no fragments to reassemble. A provider that
        emitted one anyway would contribute nothing to `parts` and be
        ignored — stated because that is a silent drop, even if an
        impossible one given the request shape.
        """
        if isinstance(event, UsageEvent):
            turn_cost = self._cost_tracker.record_turn(event.usage, event.model, span)
            self._pending_cost_usd += turn_cost.cost_usd
            return
        if isinstance(event, TokenEvent):
            content = event.delta.get("content")
            if content:
                parts.append(str(content))

    # ------------------------------------------------------------------
    # Prompt rendering (docs/11 §6 owns the prompt text)
    # ------------------------------------------------------------------

    def _render_prompt(self, departing: Sequence[BufferedTurn]) -> str:
        """Interpolate the two placeholders docs/11 §6's prompt declares.

        `str.replace`, not `str.format`: the prompt is prose owned by
        docs/11 §6, and `format` would raise `KeyError` the day someone
        adds a JSON example or a literal brace to it. `replace` keeps a
        prompt edit from being able to break this code.
        """
        previous = self._summary.text if self._summary is not None else _NO_PRIOR_SUMMARY
        return self._prompt.replace("{summary}", previous).replace(
            "{turns}", render_turns(departing)
        )


def render_turns(turns: Iterable[BufferedTurn]) -> str:
    """The `{turns}` block: one line per message, tagged with its turn
    number and a merchant-facing role label.

    Tool messages are rendered, deliberately. docs/11 §6's prompt says not
    to include "raw tool payloads", and these are not raw — what reaches a
    transcript is already the <=120-token digest `ToolExecutor` produced
    (docs/10 §2), and docs/09 §4.2 requires the *outcome of every tool
    call* to survive the fold verbatim. Dropping them here would delete
    the facts the contract is about.

    Assistant messages that only propose tool calls carry no content; they
    render as the call and its arguments, so "which tool was called" is in
    the fold's input even when the turn's prose is elsewhere.
    """
    lines: list[str] = []
    for turn in turns:
        for message in turn.messages:
            label = _ROLE_LABELS.get(message.role, message.role.value)
            if message.role is Role.TOOL:
                label = f"tool {message.name}" if message.name else "tool"
            if message.content:
                lines.append(f"[turn {turn.turn_no}] {label}: {message.content}")
            for call in message.tool_calls or ():
                lines.append(f"[turn {turn.turn_no}] {label} called {call.name}({call.arguments})")
    return "\n".join(lines)


__all__ = [
    "FIRST_FOLD_TURN",
    "FOLD_INTERVAL",
    "SUMMARY_TOKEN_BUDGET",
    "WINDOW_TURNS",
    "BufferedTurn",
    "Summarizer",
    "render_turns",
    "should_fold",
    "summary_thru_turn",
    "window_start_turn",
]
