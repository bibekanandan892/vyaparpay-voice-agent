"""`ToolExecutor` — everything between the LLM emitting a `tool_call`
and a formatted result re-entering the stream (docs/10-tool-calling.md
§2's pipeline, docs/05-agent-architecture.md §3.5). Implements
`app.domain.interfaces.ToolExecutorProto`.

Pipeline per call, in docs/10 §2's stage order: registry lookup (miss ->
denied audit + typed refusal) -> Pydantic validation (failure -> §5
validation shape, *no* audit — retried in-turn before audit) ->
`SafetyLayer.authorize_tool` (fail -> denied audit + refusal) -> tier
dispatch -> confirm gate / idempotency for mutating tiers -> execution
under a hard timeout -> synchronous audit -> result truncation. Errors
are results, never exceptions (docs/10 §5) — nothing this class returns
or raises can kill the turn.

The same-transaction resolution (registry docstring's task-4.4 handoff):

- **Mutating tiers (CONFIRM_REQUIRED / SENSITIVE / CONTROL)** execute via
  `Registry.get_raw()`: ONE session from this class's own injected
  sessionmaker carries `repo_type(session)` for the business write AND
  `ToolAuditRepo(session)` for the audit row, committed once — the OK
  audit row and the business write are truly atomic (docs/10 §2: "one
  INSERT into tool_invocations, synchronous, same transaction as the
  business write for mutating tools"). On failure (AppError / timeout /
  unexpected), the session closes uncommitted — the business write rolls
  back — and the ERROR audit row is written in a *fresh* transaction:
  there is no surviving business write to co-commit with, so a separate
  audit transaction is the correct shape there, not a gap.
- **READ tier** uses the registered 2-arg `ToolHandler` bridge (which
  opens/commits its own session), with the audit row in a separate
  transaction. Documented tradeoff: a crash between a read's execution
  and its audit insert loses one read-audit row — an observability gap,
  never a money-movement one, because reads have no business write. The
  frozen `ToolHandler` contract stays exercised on the hot read path.
- One residual non-atomicity, flagged honestly rather than hidden: the
  Redis idempotency SET and pending-confirm CLEAR happen *after* the
  Postgres commit. A crash in that window leaves a committed mutation
  whose Redis fast-path key is missing — the durable backstop is the
  `UNIQUE` constraint on `tool_invocations.idempotency_key` (docs/10
  §4.2's two-layer design), which still refuses a second row. This is
  the design docs/10 §4.2 specifies (Redis fast path + DB backstop), not
  an approximation of it; noting it here so nobody assumes the Redis key
  is the source of truth. Pending follow-up (Phase 3+, if it ever
  matters): on an idempotency-audit `IntegrityError`, re-read the
  existing row and replay it instead of surfacing an internal error.

Confirm-gate mechanics (docs/10 §4, §6) as implemented here:

- Idempotency is checked FIRST for confirm tiers, before the gate — a
  deliberate reordering of §2's flowchart arrow (gate -> idempotency):
  if this turn's decision already reached a terminal state (reconnect
  replay, in-turn retry after an error), replaying it beats re-proposing
  it, and it prevents a second same-key audit row. §4.2's "the unit of
  exactly-once is the confirmed decision, anchored to a turn" is the
  authority; the flowchart's visual order is not load-bearing.
- Execution requires `affirmed=True` AND a *matching* pending action:
  same tool, args strictly equal to the pending proposal's validated
  args (`model_dump(mode="json")` compared to `PendingConfirm.args`).
  Strict equality on purpose: "yes but make it a lakh" must never
  execute the old amount, and a changed argument is by definition a new
  proposal (docs/10 §4 step 2). `PendingConfirm.args` stores the
  *validated* dump (the §6 T5.4 Redis example shows parsed values), so
  raw-vs-coerced representation differences can't defeat the match.
- Exactly one pending at a time: a proposal that differs from an
  existing pending audits the old one `cancelled` (idempotency_key=None
  on that row — the original PENDING_CONFIRM row already holds the
  turn's key, and the column is UNIQUE) and replaces it. A re-proposal
  of the *same* action re-audits `pending_confirm` under the new turn's
  key but keeps the existing Redis pending untouched, preserving the
  original `proposed_turn`/`invocation_id` anchor (the §6 reconnect
  variant: restate, don't reset).
- The gate result reproduces §6 T5.4's shape verbatim:
  `{"status": "pending_confirm", "instruction": …, "action": {"tool":
  …, "summary": …}}`. Summaries come from a per-tool builder table
  (`request_limit_increase` gets the canonical "raise daily limit from
  ₹25,000 to ₹50,000" wording); unknown tools get a generic
  name-plus-args fallback — serviceable to voice, honest about not
  knowing the tool's domain language.
- The idempotency value stored in Redis is a JSON envelope
  `{"invocation_id", "status", "ok", "data", "error"}` for every
  terminal mutating outcome (ok AND error — §4.2's "an executor retry
  after a timeout … gets the stored result instead of a double
  submission" requires replaying errors too). A replay reconstructs the
  ToolResult from it, marks `data["idempotent_replay"] = true` (an
  extra key the LLM can see; the audit row is NOT re-written — the
  UNIQUE backstop's whole point), and logs the replay.

Other judgment calls, flagged per house style:

- A validation failure's ToolResult carries `status=ERROR` even though
  no audit row exists (docs/10 §5: "none — retried before audit"):
  `ToolInvocationStatus` has no "unaudited" member and ERROR is the §5
  table's own status for a *persistent* validation failure. The absence
  of an audit row is the contract; the enum value is just the envelope's
  nearest mirror.
- Separate-transaction audits (`_audit`) are best-effort: an audit
  INSERT failure is logged loudly (`log.exception`) and the pipeline
  continues, because failing the caller's read over a lost audit row
  inverts the harm. The mutating OK path is the exception — there the
  audit is inside the business transaction, so its failure rolls the
  mutation back too, which is exactly the guarantee docs/10 §2 asks for.
- Per-tool validation hints mirror §6 T5.2's model-facing hint for the
  canonical tool; tools without an entry get Pydantic's field messages
  alone.
- SENSITIVE is dispatched exactly like CONFIRM_REQUIRED (no Phase-2
  sensitive tools exist; the ReAuth sub-state of docs/10 §4.1 slots in
  front of `affirmed` later without reshaping dispatch). CONTROL
  executes immediately (docs/10 §7 — no gate, no idempotency key), via
  the same-transaction path since control tools still write (the
  hand-off record). Batch parallelism is READ-only: any non-READ call
  serializes the whole batch (docs/10 §2's rule stated for mutating
  calls, applied to CONTROL too — interleavings are not worth reasoning
  about).
- Result truncation (docs/10 §2 format stage) reuses
  `session_memory.py`'s ~120-token-as-480-chars proxy: top-level list
  values are cut to 5 rows with `{"truncated": true, "total_available":
  n}` appended; if the rendered JSON still exceeds the cap the data is
  replaced by `{"truncated": true, "digest": "<prefix>…"}` — the digest
  is a *string field inside valid JSON*, so truncation can never emit
  corrupt JSON to the LLM. Audit rows store the FULL output; only the
  conversation-bound copy is truncated.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Final

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.errors import AppError
from app.data.redis_client import RedisClient
from app.data.repositories.tool_audit_repo import ToolAuditRepo
from app.domain.interfaces import RegisteredTool, SafetyLayerProto
from app.domain.types import (
    PendingConfirm,
    SessionUser,
    Tier,
    ToolCall,
    ToolInvocationStatus,
    ToolResult,
)
from app.obs.logging import get_logger
from app.obs.tracing import safe_set_attribute, tool_span
from app.tools.errors import business_error, internal_error, timeout_error, validation_error
from app.tools.registry import RawRegistration, Registry, configure

log = get_logger(__name__)

# docs/10 §2's Execute-stage budget: `asyncio.wait_for(handler, 2.0)`.
_EXECUTION_TIMEOUT_S: Final = 2.0
# docs/10 §2 format stage: 5-row list truncation, ~120-token render cap —
# the same chars-as-token-proxy convention as session_memory.py's
# _MAX_DIGEST_CHARS (no tokenizer at this layer).
_MAX_LIST_ROWS: Final = 5
_MAX_RESULT_CHARS: Final = 480
_TRUNCATION_SUFFIX: Final = "…(truncated)"

# docs/10 §6 T5.4's instruction string, verbatim.
_GATE_INSTRUCTION: Final = (
    "State the action and its consequence, then ask for explicit confirmation. "
    "Do not treat this as executed."
)

# docs/10 §5's authorization-class codes (audit status `denied`).
_TOOL_NOT_ALLOWED: Final = "TOOL_NOT_ALLOWED"
_OUT_OF_SCOPE: Final = "OUT_OF_SCOPE"
_INTERNAL_CODE: Final = "INTERNAL"
_TIMEOUT_CODE: Final = "TOOL_TIMEOUT"

# Per-tool, model-facing validation hints (docs/10 §5: "written for the
# model, not a human" — converts a retry loop into a single retry).
_VALIDATION_HINTS: Final[dict[str, str]] = {
    "request_limit_increase": "amounts are integer rupees, e.g. 50000",
}


def _summarize_request_limit_increase(args: dict[str, Any]) -> str:
    # §6 T5.4's canonical wording. `{:,}` comma-grouping matches the
    # doc's ₹25,000/₹50,000 rendering for Phase-2 magnitudes.
    return (
        f"raise daily limit from ₹{args['current_limit']:,} "
        f"to ₹{args['requested_limit']:,}"
    )


_SUMMARY_BUILDERS: Final[dict[str, Any]] = {
    "request_limit_increase": _summarize_request_limit_increase,
}


def _action_summary(tool_name: str, args: dict[str, Any]) -> str:
    """Human summary for the gate's `action.summary` — per-tool builder
    when one exists, generic name+args fallback otherwise (documented in
    the module docstring). A builder crash falls back rather than
    breaking the gate."""
    builder = _SUMMARY_BUILDERS.get(tool_name)
    if builder is not None:
        try:
            return str(builder(args))
        except Exception:
            log.exception("gate_summary_builder_failed", tool=tool_name)
    rendered_args = json.dumps(args, separators=(", ", "="), default=str)
    return f"execute {tool_name} with arguments {rendered_args}"


def _refusal_error(code: str, message: str) -> dict[str, Any]:
    """docs/10 §5's authorization-class refusal, rendered in the business
    wire shape (the §5 table's three shapes have no fourth
    'authorization' rendering; `business` is the one that carries a
    voiceable `detail`)."""
    return {
        "type": "business",
        "code": code,
        "detail": {"message": message},
        "retryable": False,
    }


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _idempotency_key(session_id: str, tool_name: str, turn_no: int) -> str:
    """docs/10 §4.2's composite key, stored plain in the audit column;
    `RedisClient` owns the Redis-side `idempotency:` prefixing."""
    return f"{session_id}:{tool_name}:{turn_no}"


def _truncate_data(data: dict[str, Any]) -> dict[str, Any]:
    """docs/10 §2's format stage (module docstring: "Result
    truncation"). Never mutates `data`; never emits corrupt JSON."""
    out = dict(data)
    for key, value in data.items():
        if isinstance(value, list) and len(value) > _MAX_LIST_ROWS:
            out[key] = value[:_MAX_LIST_ROWS]
            out["truncated"] = True
            out["total_available"] = len(value)
    rendered = json.dumps(out, separators=(",", ":"), default=str)
    if len(rendered) <= _MAX_RESULT_CHARS:
        return out
    return {"truncated": True, "digest": rendered[:_MAX_RESULT_CHARS] + _TRUNCATION_SUFFIX}


class ToolExecutor:
    """Implements `ToolExecutorProto` (app/domain/interfaces.py). See the
    module docstring for the pipeline, the same-transaction resolution,
    and every judgment call."""

    def __init__(
        self,
        *,
        registry: Registry,
        safety: SafetyLayerProto,
        redis: RedisClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        execution_timeout_s: float = _EXECUTION_TIMEOUT_S,
    ) -> None:
        """`registry` is typed as the *concrete* `Registry`, not the
        `ToolRegistry` Protocol — this class needs `get_raw()`, the
        registry-side extension beyond the Protocol floor.
        `execution_timeout_s` defaults to docs/10 §2's 2 s budget; it is
        a parameter only as a test seam, never overridden in production
        wiring. The constructor also calls `app.tools.registry.
        configure(sessionmaker)` — the registry docstring names this
        class as one of the two sanctioned owners of that call — so the
        READ-path bridge closures draw sessions from the SAME pool as
        the mutating path. (Module-global wiring: a second executor with
        a different sessionmaker would re-point the bridge; Phase 2 runs
        exactly one executor per process.)"""
        self._registry = registry
        self._safety = safety
        self._redis = redis
        self._sessionmaker = sessionmaker
        self._execution_timeout_s = execution_timeout_s
        configure(sessionmaker)

    async def execute(
        self,
        calls: list[ToolCall],
        *,
        principal: SessionUser,
        session_id: str,
        turn_no: int,
        affirmed: bool = False,
    ) -> list[ToolResult]:
        """docs/10 §2: read-only batches run in parallel
        (`asyncio.gather`); any non-READ call serializes the whole batch
        (unknown tools don't affect the decision — they never execute).
        Results come back in call order either way."""
        if not calls:
            return []
        registered = (self._registry.get(call.name) for call in calls)
        only_reads = all(entry is None or entry.tier == Tier.READ for entry in registered)
        if only_reads and len(calls) > 1:
            return list(
                await asyncio.gather(
                    *(
                        self._execute_one(
                            call,
                            principal=principal,
                            session_id=session_id,
                            turn_no=turn_no,
                            affirmed=affirmed,
                        )
                        for call in calls
                    )
                )
            )
        return [
            await self._execute_one(
                call,
                principal=principal,
                session_id=session_id,
                turn_no=turn_no,
                affirmed=affirmed,
            )
            for call in calls
        ]

    # ------------------------------------------------------------------
    # Per-call pipeline
    # ------------------------------------------------------------------

    async def _execute_one(
        self,
        call: ToolCall,
        *,
        principal: SessionUser,
        session_id: str,
        turn_no: int,
        affirmed: bool,
    ) -> ToolResult:
        """One `tool.exec.<name>` span per call (docs/05 §3.10), and the
        errors-as-results boundary: whatever `_pipeline` raises (executor
        bugs, Redis outages — handler failures are already caught deeper)
        becomes an internal-error result, never an exception to the
        caller."""
        start = time.monotonic()
        with tool_span(call.name) as span:
            try:
                result = await self._pipeline(
                    call,
                    principal=principal,
                    session_id=session_id,
                    turn_no=turn_no,
                    affirmed=affirmed,
                    start=start,
                )
            except Exception:
                log.exception(
                    "tool_executor_internal_error", tool=call.name, session_id=session_id
                )
                elapsed = _elapsed_ms(start)
                await self._audit(
                    session_id=session_id,
                    tool_name=call.name,
                    input=call.arguments,
                    status=ToolInvocationStatus.ERROR,
                    latency_ms=elapsed,
                    turn_no=turn_no,
                    error_code=_INTERNAL_CODE,
                )
                result = ToolResult(
                    tool_call_id=call.id,
                    tool_name=call.name,
                    ok=False,
                    error=internal_error(),
                    status=ToolInvocationStatus.ERROR,
                    latency_ms=elapsed,
                )
            safe_set_attribute(span, "tool", call.name)
            safe_set_attribute(span, "turn_no", turn_no)
            safe_set_attribute(span, "status", result.status.value)
            safe_set_attribute(span, "latency_ms", result.latency_ms)
            if result.idempotency_key is not None:
                safe_set_attribute(span, "idempotency_key", result.idempotency_key)
            return result

    async def _pipeline(
        self,
        call: ToolCall,
        *,
        principal: SessionUser,
        session_id: str,
        turn_no: int,
        affirmed: bool,
        start: float,
    ) -> ToolResult:
        """docs/10 §2's stages in order: lookup -> validate -> authorize
        -> tier dispatch."""
        registered = self._registry.get(call.name)
        if registered is None:
            return await self._deny(
                call,
                code=_TOOL_NOT_ALLOWED,
                message=f"tool {call.name!r} is not in the allowlist",
                session_id=session_id,
                turn_no=turn_no,
                start=start,
            )
        try:
            args = registered.input_model.model_validate(call.arguments)
        except ValidationError as exc:
            # docs/10 §5: no audit row — the LLM retries in-turn before
            # audit (worked example §6 T5.2). Status choice documented in
            # the module docstring.
            return ToolResult(
                tool_call_id=call.id,
                tool_name=call.name,
                ok=False,
                error=validation_error(exc, hint=_VALIDATION_HINTS.get(call.name)),
                status=ToolInvocationStatus.ERROR,
                latency_ms=_elapsed_ms(start),
            )
        if not self._safety.authorize_tool(call, principal):
            return await self._deny(
                call,
                code=_OUT_OF_SCOPE,
                message="call rejected by authorization policy",
                session_id=session_id,
                turn_no=turn_no,
                start=start,
            )
        if registered.tier == Tier.READ:
            return await self._run_read(
                call, registered, args, principal=principal, session_id=session_id,
                turn_no=turn_no, start=start,
            )
        if registered.tier == Tier.CONTROL:
            # docs/10 §7: execute immediately, no gate, no idempotency key.
            result, _ = await self._run_mutating(
                call, args, principal=principal, session_id=session_id,
                turn_no=turn_no, idem_key=None, start=start,
            )
            return result
        # CONFIRM_REQUIRED — and SENSITIVE, handled identically in Phase 2
        # (module docstring).
        return await self._confirm_tier(
            call, args, principal=principal, session_id=session_id,
            turn_no=turn_no, affirmed=affirmed, start=start,
        )

    # ------------------------------------------------------------------
    # Confirm gate + idempotency (docs/10 §4, §6)
    # ------------------------------------------------------------------

    async def _confirm_tier(
        self,
        call: ToolCall,
        args: BaseModel,
        *,
        principal: SessionUser,
        session_id: str,
        turn_no: int,
        affirmed: bool,
        start: float,
    ) -> ToolResult:
        idem_key = _idempotency_key(session_id, call.name, turn_no)
        stored = await self._redis.get_idempotency(session_id, call.name, turn_no)
        if stored is not None:
            return self._replay(call, stored, idem_key, start)
        pending = await self._redis.get_pending_confirm(session_id)
        validated = args.model_dump(mode="json")
        matches = (
            pending is not None and pending.tool == call.name and pending.args == validated
        )
        if not (affirmed and matches):
            return await self._hold_gate(
                call, validated, pending, matches,
                session_id=session_id, turn_no=turn_no, idem_key=idem_key, start=start,
            )
        result, invocation_id = await self._run_mutating(
            call, args, principal=principal, session_id=session_id,
            turn_no=turn_no, idem_key=idem_key, start=start,
        )
        # Terminal outcome (ok or error): store the replay envelope, then
        # clear the consumed pending decision. Ordering + the crash
        # window here are analyzed in the module docstring.
        envelope = {
            "invocation_id": invocation_id,
            "status": result.status.value,
            "ok": result.ok,
            "data": result.data,
            "error": result.error,
        }
        await self._redis.set_idempotency(
            session_id, call.name, turn_no, json.dumps(envelope, default=str)
        )
        await self._redis.set_pending_confirm(session_id, None)
        return result

    async def _hold_gate(
        self,
        call: ToolCall,
        validated: dict[str, Any],
        pending: PendingConfirm | None,
        matches: bool,
        *,
        session_id: str,
        turn_no: int,
        idem_key: str,
        start: float,
    ) -> ToolResult:
        """Write/refresh the pending state instead of executing (docs/10
        §4 step 1; §6 T5.4). Supersession and the matching-re-proposal
        behavior are documented in the module docstring."""
        if pending is not None and not matches:
            await self._audit(
                session_id=session_id,
                tool_name=pending.tool,
                input=pending.args,
                status=ToolInvocationStatus.CANCELLED,
                latency_ms=_elapsed_ms(start),
                turn_no=turn_no,
            )
            log.info(
                "pending_confirm_superseded",
                session_id=session_id,
                old_tool=pending.tool,
                new_tool=call.name,
            )
        elapsed = _elapsed_ms(start)
        invocation_id = await self._audit(
            session_id=session_id,
            tool_name=call.name,
            input=call.arguments,
            status=ToolInvocationStatus.PENDING_CONFIRM,
            latency_ms=elapsed,
            turn_no=turn_no,
            idempotency_key=idem_key,
        )
        if not matches:
            await self._redis.set_pending_confirm(
                session_id,
                PendingConfirm(
                    tool=call.name,
                    args=validated,
                    proposed_turn=turn_no,
                    # "" only if the best-effort audit lost the row (already
                    # logged loudly there); the pending stays actionable —
                    # matching keys off tool+args, never off this id.
                    invocation_id=invocation_id or "",
                ),
            )
        gate = {
            "status": "pending_confirm",
            "instruction": _GATE_INSTRUCTION,
            "action": {"tool": call.name, "summary": _action_summary(call.name, validated)},
        }
        return ToolResult(
            tool_call_id=call.id,
            tool_name=call.name,
            ok=False,
            gate=gate,
            status=ToolInvocationStatus.PENDING_CONFIRM,
            latency_ms=elapsed,
            idempotency_key=idem_key,
        )

    def _replay(self, call: ToolCall, stored: str, idem_key: str, start: float) -> ToolResult:
        """Idempotency hit: reconstruct the stored terminal result
        (module docstring: envelope shape, `idempotent_replay` marker, no
        second audit row)."""
        envelope = json.loads(stored)
        data = envelope.get("data")
        if isinstance(data, dict):
            data = {**data, "idempotent_replay": True}
        log.info("idempotency_replay", tool=call.name, idempotency_key=idem_key)
        return ToolResult(
            tool_call_id=call.id,
            tool_name=call.name,
            ok=bool(envelope.get("ok")),
            data=data,
            error=envelope.get("error"),
            status=ToolInvocationStatus(envelope.get("status", "ok")),
            latency_ms=_elapsed_ms(start),
            idempotency_key=idem_key,
        )

    # ------------------------------------------------------------------
    # Execution paths
    # ------------------------------------------------------------------

    async def _run_read(
        self,
        call: ToolCall,
        registered: RegisteredTool,
        args: BaseModel,
        *,
        principal: SessionUser,
        session_id: str,
        turn_no: int,
        start: float,
    ) -> ToolResult:
        """READ tier: the frozen 2-arg `ToolHandler` bridge (its own
        session/commit), audit in a separate transaction — the
        documented-fine half of the same-transaction resolution."""
        try:
            output = await asyncio.wait_for(
                registered.handler(principal, args), timeout=self._execution_timeout_s
            )
        except AppError as err:
            return await self._failure_result(
                call, business_error(err), error_code=err.code,
                session_id=session_id, turn_no=turn_no, idem_key=None, start=start,
            )
        except TimeoutError:
            elapsed = _elapsed_ms(start)
            return await self._failure_result(
                call, timeout_error(elapsed), error_code=_TIMEOUT_CODE,
                session_id=session_id, turn_no=turn_no, idem_key=None, start=start,
            )
        except Exception:
            log.exception("tool_handler_unexpected_error", tool=call.name)
            return await self._failure_result(
                call, internal_error(), error_code=_INTERNAL_CODE,
                session_id=session_id, turn_no=turn_no, idem_key=None, start=start,
            )
        full_output = output.model_dump(mode="json")
        elapsed = _elapsed_ms(start)
        await self._audit(
            session_id=session_id,
            tool_name=call.name,
            input=call.arguments,
            status=ToolInvocationStatus.OK,
            latency_ms=elapsed,
            turn_no=turn_no,
            output=full_output,
        )
        return ToolResult(
            tool_call_id=call.id,
            tool_name=call.name,
            ok=True,
            data=_truncate_data(full_output),
            status=ToolInvocationStatus.OK,
            latency_ms=elapsed,
        )

    async def _run_mutating(
        self,
        call: ToolCall,
        args: BaseModel,
        *,
        principal: SessionUser,
        session_id: str,
        turn_no: int,
        idem_key: str | None,
        start: float,
    ) -> tuple[ToolResult, str | None]:
        """Mutating tiers: the same-transaction core. One session carries
        the raw handler's business write AND the OK audit row, committed
        together; any failure exits the `async with` uncommitted (the
        business write rolls back) and audits ERROR in a fresh
        transaction. Returns `(result, invocation_id)` — the id feeds the
        idempotency envelope (docs/10 §4.2 "stores invocation_id +
        status")."""
        raw = self._registry.get_raw(call.name)
        if raw is None:
            # Registered without @tool (impossible via the decorator);
            # fail closed as an internal error rather than guessing.
            log.error("registered_tool_missing_raw_handler", tool=call.name)
            result = await self._failure_result(
                call, internal_error(), error_code=_INTERNAL_CODE,
                session_id=session_id, turn_no=turn_no, idem_key=idem_key, start=start,
            )
            return result, None
        try:
            return await self._commit_mutation(
                call, raw, args, principal=principal, session_id=session_id,
                turn_no=turn_no, idem_key=idem_key, start=start,
            )
        except AppError as err:
            result = await self._failure_result(
                call, business_error(err), error_code=err.code,
                session_id=session_id, turn_no=turn_no, idem_key=idem_key, start=start,
            )
            return result, None
        except TimeoutError:
            elapsed = _elapsed_ms(start)
            result = await self._failure_result(
                call, timeout_error(elapsed), error_code=_TIMEOUT_CODE,
                session_id=session_id, turn_no=turn_no, idem_key=idem_key, start=start,
            )
            return result, None
        except Exception:
            log.exception("tool_handler_unexpected_error", tool=call.name)
            result = await self._failure_result(
                call, internal_error(), error_code=_INTERNAL_CODE,
                session_id=session_id, turn_no=turn_no, idem_key=idem_key, start=start,
            )
            return result, None

    async def _commit_mutation(
        self,
        call: ToolCall,
        raw: RawRegistration,
        args: BaseModel,
        *,
        principal: SessionUser,
        session_id: str,
        turn_no: int,
        idem_key: str | None,
        start: float,
    ) -> tuple[ToolResult, str | None]:
        """The one-transaction happy path; exceptions propagate to
        `_run_mutating`'s handlers with the session already closed
        (uncommitted work discarded)."""
        async with self._sessionmaker() as session:
            repo = raw.repo_type(session)
            output = await asyncio.wait_for(
                raw.raw_handler(principal, args, repo), timeout=self._execution_timeout_s
            )
            full_output = output.model_dump(mode="json")
            elapsed = _elapsed_ms(start)
            row = await ToolAuditRepo(session).insert(
                session_id=session_id,
                tool_name=call.name,
                input=call.arguments,
                output=full_output,
                status=ToolInvocationStatus.OK.value,
                latency_ms=elapsed,
                turn_no=turn_no,
                idempotency_key=idem_key,
            )
            await session.commit()
            invocation_id = str(row.invocation_id) if row.invocation_id is not None else None
        result = ToolResult(
            tool_call_id=call.id,
            tool_name=call.name,
            ok=True,
            data=_truncate_data(full_output),
            status=ToolInvocationStatus.OK,
            latency_ms=elapsed,
            idempotency_key=idem_key,
        )
        return result, invocation_id

    # ------------------------------------------------------------------
    # Shared failure/deny/audit plumbing
    # ------------------------------------------------------------------

    async def _failure_result(
        self,
        call: ToolCall,
        error: dict[str, Any],
        *,
        error_code: str,
        session_id: str,
        turn_no: int,
        idem_key: str | None,
        start: float,
    ) -> ToolResult:
        """One ERROR-status audit row + the errors-as-results envelope,
        shared by every failure class (docs/10 §5)."""
        elapsed = _elapsed_ms(start)
        await self._audit(
            session_id=session_id,
            tool_name=call.name,
            input=call.arguments,
            status=ToolInvocationStatus.ERROR,
            latency_ms=elapsed,
            turn_no=turn_no,
            error_code=error_code,
            idempotency_key=idem_key,
        )
        return ToolResult(
            tool_call_id=call.id,
            tool_name=call.name,
            ok=False,
            error=error,
            status=ToolInvocationStatus.ERROR,
            latency_ms=elapsed,
            idempotency_key=idem_key,
        )

    async def _deny(
        self,
        call: ToolCall,
        *,
        code: str,
        message: str,
        session_id: str,
        turn_no: int,
        start: float,
    ) -> ToolResult:
        """docs/10 §2: denial is "a signal worth recording, not an
        exception" — one `denied` audit row (no error_code: the DB CHECK
        pairs error_code with status='error' only) + a typed refusal."""
        elapsed = _elapsed_ms(start)
        await self._audit(
            session_id=session_id,
            tool_name=call.name,
            input=call.arguments,
            status=ToolInvocationStatus.DENIED,
            latency_ms=elapsed,
            turn_no=turn_no,
        )
        return ToolResult(
            tool_call_id=call.id,
            tool_name=call.name,
            ok=False,
            error=_refusal_error(code, message),
            status=ToolInvocationStatus.DENIED,
            latency_ms=elapsed,
        )

    async def _audit(
        self,
        *,
        session_id: str,
        tool_name: str,
        input: dict[str, Any],
        status: ToolInvocationStatus,
        latency_ms: int,
        turn_no: int,
        output: dict[str, Any] | None = None,
        error_code: str | None = None,
        idempotency_key: str | None = None,
    ) -> str | None:
        """Separate-transaction audit insert for every non-mutating-OK
        outcome (the mutating OK row is written inside `_commit_mutation`
        instead). Best-effort by design — see the module docstring's
        judgment call; a failure is logged loudly, never propagated.
        `input` shadows the builtin to mirror `ToolAuditRepo.insert`'s
        own column-named parameter."""
        try:
            async with self._sessionmaker() as session:
                row = await ToolAuditRepo(session).insert(
                    session_id=session_id,
                    tool_name=tool_name,
                    input=input,
                    status=status.value,
                    latency_ms=latency_ms,
                    turn_no=turn_no,
                    output=output,
                    error_code=error_code,
                    idempotency_key=idempotency_key,
                )
                await session.commit()
                return str(row.invocation_id) if row.invocation_id is not None else None
        except Exception:
            log.exception(
                "tool_audit_write_failed",
                tool=tool_name,
                session_id=session_id,
                status=status.value,
            )
            return None


__all__ = ["ToolExecutor"]
