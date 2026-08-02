"""The `@tool` decorator + the allowlist registry `ToolExecutor` (Phase-2
plan Batch 4, task 4.4 — out of scope here) looks tool names up in
(docs/10-tool-calling.md §2 "Registry lookup" stage, §8 "Adding tool
#17").

Two responsibilities live here, deliberately kept small:

1. **The catalog.** `@tool(...)` registers a name -> `app.domain.
   interfaces.RegisteredTool` mapping (tier, latency class, input/output
   Pydantic models, the callable) in a process-wide dict. `Registry`
   implements `app.domain.interfaces.ToolRegistry` (`.get`/`.all`/
   `.to_openai_tools`) against that dict — the read side every future
   consumer (`ToolExecutor`, `LLMRouter`/`ConversationManager` building
   the `tools` array for `LLMProvider.stream()`) uses.

2. **The `ToolHandler`-shape bridge.** `app.domain.interfaces.
   ToolHandler` is frozen as a strict 2-argument callable — `(principal,
   args) -> BaseModel` (docs/10 §1 invariant 3: no tool ever accepts a
   principal-identifying argument, so nothing about *reaching the
   database* belongs in that shape either). But per the Phase-2 plan's
   decision #4, tool handlers call repositories directly, in-process —
   which means each handler genuinely needs a repo instance built from a
   live `AsyncSession` to do anything. This module resolves that tension
   instead of leaving each tool file to invent its own answer: every
   `@tool`-decorated function is written as a 3-argument
   `(principal, args, repo)` coroutine (directly unit-testable with a
   mocked repo — see backend/tests/tools/), and the decorator wraps it,
   at registration time, into the exact 2-argument closure the registry
   stores and `ToolHandler` requires. The decorator returns the original
   3-arg function unchanged as the module-level name, so
   `from app.tools.get_wallet_balance import get_wallet_balance` in a
   test still gets the directly-callable, repo-parametrized version.

   This is a narrow, intentional deviation from `ToolHandler`'s literal
   shape, not silent drift: `app/domain/interfaces.py`'s own docstring
   says a deviation should update that file first, but this task's scope
   is explicitly limited to `app/tools/*` (parallel agents own the rest
   of the Phase-2 diff), so the reconciliation — most likely giving
   `ToolExecutor` a session/repo-factory of its own rather than changing
   `ToolHandler`'s signature — is left for whoever builds `ToolExecutor`
   (task 4.4) to resolve with the interface's owner.

   **Resolved by task 4.4** exactly the way that paragraph anticipated —
   `ToolExecutor` (app/agent/tool_executor.py) has its own injected
   `async_sessionmaker`; neither `ToolHandler` nor the frozen
   `RegisteredTool` changed shape. The one registry-side addition is
   `_RAW_HANDLERS`/`RawRegistration`/`Registry.get_raw()` below: the
   decorator now *also* records the original 3-arg `(principal, args,
   repo)` coroutine plus its `repo_type`, keyed by the same name as
   `_REGISTRY`. Why: docs/10 §2's audit stage requires a mutating tool's
   `tool_invocations` row in the SAME transaction as its business write,
   but this module's 2-arg bridge closure opens its own session and
   commits internally — an audit row written around it would always be a
   second transaction. `ToolExecutor` therefore executes mutating tiers
   via `get_raw()`: it opens ONE session from its own sessionmaker,
   builds `repo_type(session)` AND the audit repo on that same session,
   runs the raw handler, inserts the audit row, and commits once — true
   single-transaction coupling. READ tools keep using the registered
   2-arg `handler` (this bridge), because a read has no business write
   to co-commit and the bridge is the frozen `ToolHandler` contract
   working as designed. Note for test authors: fixtures that
   snapshot/restore `_REGISTRY` should snapshot `_RAW_HANDLERS` too;
   an orphaned `_RAW_HANDLERS` entry is benign (every executor lookup
   goes through `_REGISTRY` first, and re-registering a name overwrites
   both dicts) but tidiness beats surprise.

   **The session the bridge closure uses is injected, not self-built**
   (fixed after review — an earlier version called
   `create_engine_and_sessionmaker(get_settings())` here directly,
   building a second, independent connection pool the app's lifespan
   (`app/api/main.py`) never knew about and never disposed, with no way
   for a test to point it at anything but a real database). `configure()`
   below is the one place a real `async_sessionmaker` gets wired in —
   `app/api/main.py`'s lifespan (or `ToolExecutor`, task 4.4, whichever
   ends up owning this call) is expected to call it once at startup with
   the SAME sessionmaker `app.state.sessionmaker` uses, not a second one.
   Calling a registered tool before `configure()` has run raises a clear
   `RuntimeError` rather than silently opening an unmanaged pool.

Docs: docs/10-tool-calling.md §2 (pipeline stages), §8 (the `@tool`
pattern, worked example `get_fee_summary`); docs/04-backend-
architecture.md §4-5 (one `AsyncSession` per caller; repositories take a
session in their constructor and never open their own).
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, cast, get_type_hints

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.data.repositories.base import SqlAlchemyRepository
from app.domain.interfaces import RegisteredTool, ToolHandler
from app.domain.types import SessionUser, Tier

type RawHandler[InT: BaseModel, OutT: BaseModel, RepoT: SqlAlchemyRepository[Any]] = Callable[
    [SessionUser, InT, RepoT], Awaitable[OutT]
]

_REGISTRY: dict[str, RegisteredTool] = {}

# Type-erased view of a raw 3-arg handler as stored in _RAW_HANDLERS.
# Erased (InT -> BaseModel, RepoT -> Any) for the same reason _wrapped's
# own cast exists: `Callable` is contravariant in its parameters, so the
# per-tool `RawHandler[InT, OutT, RepoT]` cannot be assigned to a common
# storage type without widening. Safe for the same reason too — the one
# caller (`ToolExecutor.get_raw` path) validates args against this tool's
# `input_model` and constructs `repo_type(session)` before invoking.
type ErasedRawHandler = Callable[[SessionUser, BaseModel, Any], Awaitable[BaseModel]]


@dataclass(frozen=True)
class RawRegistration:
    """The pre-bridge registration record task 4.4's same-transaction
    execution path consumes (see the module docstring's "Resolved by
    task 4.4" paragraph): the original 3-arg `(principal, args, repo)`
    coroutine and the repo class to build from the *caller's* session —
    letting `ToolExecutor` co-commit a mutating tool's business write
    with its audit row in one transaction (docs/10 §2's audit stage).
    Lives here (not in the frozen `app/domain/interfaces.py`) because it
    is a registry implementation detail, not a shared contract."""

    raw_handler: ErasedRawHandler
    repo_type: type[SqlAlchemyRepository[Any]]


_RAW_HANDLERS: dict[str, RawRegistration] = {}

_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def configure(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """Wire the registry's tool-invocation bridge to a real, already-
    lifecycle-managed session factory — call once at startup with the
    same `async_sessionmaker` the app's lifespan built and will
    `.dispose()` on shutdown (`app/data/engine.create_engine_and_
    sessionmaker`'s one production caller). Deliberately does NOT build
    its own engine here: this module has no `.dispose()` hook of its own
    to call, so owning a second pool would leak it.
    """
    global _sessionmaker
    _sessionmaker = sessionmaker


def _extract_io_models(
    handler: Callable[..., Awaitable[BaseModel]],
) -> tuple[type[BaseModel], type[BaseModel]]:
    """Pulls the Pydantic input/output models straight from the raw
    handler's own annotations — `args: SomeIn` (the second parameter;
    the first is always `principal`) and `-> SomeOut` — rather than
    taking them as separate decorator keyword arguments. One source of
    truth: docs/10 §8's worked example spells the models out only in the
    function signature (`async def get_fee_summary(principal:
    SessionUser, args: GetFeeSummaryIn) -> GetFeeSummaryOut`), and a
    handler's signature can't drift from itself.
    """
    hints = get_type_hints(handler)
    args_param_name = list(inspect.signature(handler).parameters)[1]
    input_model = hints[args_param_name]
    output_model = hints["return"]
    if not (isinstance(input_model, type) and issubclass(input_model, BaseModel)):
        raise TypeError(f"{handler.__name__}: 'args' parameter must be a BaseModel subclass")
    if not (isinstance(output_model, type) and issubclass(output_model, BaseModel)):
        raise TypeError(f"{handler.__name__}: return type must be a BaseModel subclass")
    return input_model, output_model


def tool[InT: BaseModel, OutT: BaseModel, RepoT: SqlAlchemyRepository[Any]](
    *,
    name: str,
    tier: Tier,
    latency_class: Literal["read-single", "write"],
    repo_type: type[RepoT],
    description: str = "",
) -> Callable[[RawHandler[InT, OutT, RepoT]], RawHandler[InT, OutT, RepoT]]:
    """docs/10 §8's decorator pattern. `repo_type` is this module's own
    addition (see the module docstring's "ToolHandler-shape bridge"):
    the concrete `SqlAlchemyRepository` subclass the decorator builds
    from a fresh `AsyncSession` — `repo_type(session)` — on every call,
    then hands to `raw_handler` as its third argument. Required, not
    optional: every Phase-2 tool calls a repository (plan decision #4),
    so there is no handler shape here that doesn't need one yet.

    `InT`/`OutT` are inferred by mypy from `raw_handler`'s own
    annotations at the `@tool(...)` call site — carrying them as
    explicit type parameters (rather than collapsing both to the
    `ToolHandler` Protocol's bound, `BaseModel`) is what lets
    `get_wallet_balance` keep typing as
    `Callable[[SessionUser, GetWalletBalanceIn, WalletRepo],
    Awaitable[GetWalletBalanceOut]]` after decoration, instead of mypy
    widening its return type to the bare `BaseModel` every caller
    (including this module's own tests) would then have to re-narrow.

    Raises `ValueError` on a duplicate `name` — two tools silently
    sharing a registry slot would let the second overwrite the first,
    and `registry.get(name)` would return the wrong handler with no
    signal that anything was wrong.
    """

    def decorator(raw_handler: RawHandler[InT, OutT, RepoT]) -> RawHandler[InT, OutT, RepoT]:
        if name in _REGISTRY:
            raise ValueError(f"tool {name!r} is already registered")

        input_model, output_model = _extract_io_models(raw_handler)

        async def _wrapped(principal: SessionUser, args: BaseModel) -> BaseModel:
            if _sessionmaker is None:
                raise RuntimeError(
                    "app.tools.registry.configure(sessionmaker) must be called before "
                    f"invoking tool {name!r} — no session factory is wired yet. This is "
                    "almost certainly a startup-wiring bug, not a per-call one: nothing "
                    "in this module builds its own connection pool as a fallback."
                )
            async with _sessionmaker() as session:
                repo = repo_type(session)
                # `args` is `BaseModel` here only because `ToolHandler`
                # (app/domain/interfaces.py) fixes that exact parameter
                # type for every tool alike -- the real caller (the
                # future ToolExecutor) only ever reaches this closure
                # after validating `args` against this tool's own
                # `input_model` (docs/10 §2's schema-validation stage,
                # which runs before Execute), so it is always actually
                # an `InT` instance by the time it gets here.
                result = await raw_handler(principal, cast(InT, args), repo)
                await session.commit()
                return result

        handler: ToolHandler = _wrapped

        _REGISTRY[name] = RegisteredTool(
            name=name,
            tier=tier,
            latency_class=latency_class,
            input_model=input_model,
            output_model=output_model,
            handler=handler,
            description=description,
        )
        # Kept adjacent to the _REGISTRY insert on purpose: the two dicts
        # share a key and must never drift. The cast erases InT/RepoT for
        # storage — see ErasedRawHandler's comment for why that is safe.
        _RAW_HANDLERS[name] = RawRegistration(
            raw_handler=cast(ErasedRawHandler, raw_handler),
            repo_type=cast(type[SqlAlchemyRepository[Any]], repo_type),
        )
        return raw_handler

    return decorator


class Registry:
    """Implements `app.domain.interfaces.ToolRegistry` against the
    module-level `_REGISTRY` dict every `@tool`-decorated function
    populates as an import side effect. Not named `ToolRegistry` itself
    to avoid colliding with the Protocol of the same name in
    `app.domain.interfaces` — callers that want the Protocol type import
    it from there; this is the one concrete implementation of it.
    """

    def get(self, name: str) -> RegisteredTool | None:
        return _REGISTRY.get(name)

    def get_raw(self, name: str) -> RawRegistration | None:
        """Task 4.4's same-transaction seam — a concrete-`Registry`
        extension *beyond* the `ToolRegistry` Protocol floor (the
        Protocol stays frozen; docs/04's "floor, not ceiling"
        convention). Only `ToolExecutor`'s mutating-tier path should
        call this; every other consumer wants `get()`'s bridged
        handler."""
        return _RAW_HANDLERS.get(name)

    def all(self) -> list[RegisteredTool]:
        return list(_REGISTRY.values())

    def to_openai_tools(self) -> list[dict[str, Any]]:
        """The `tools: [...]` array `LLMProvider.stream()` takes
        (`app.domain.interfaces.LLMProvider`) — OpenAI-compatible
        function-calling shape, matching `Message.to_wire()`'s own
        `tool_calls` rendering (`app/domain/types.py`)."""
        return [
            {
                "type": "function",
                "function": {
                    "name": registered.name,
                    "description": registered.description,
                    "parameters": registered.input_model.model_json_schema(),
                },
            }
            for registered in _REGISTRY.values()
        ]


registry = Registry()

__all__ = ["RawRegistration", "Registry", "configure", "registry", "tool"]
