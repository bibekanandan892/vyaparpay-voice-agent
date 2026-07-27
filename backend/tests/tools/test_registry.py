"""Unit tests for `app/tools/registry.py` (docs/10-tool-calling.md §2,
§8). Importing `app.tools` is what registers the 3 Phase-2 tools as a
side effect — every test in this module relies on that import having
already run.
"""

from __future__ import annotations

import importlib

import pytest
from pydantic import BaseModel

import app.tools  # noqa: F401 -- import side effect: registers all 3 Phase-2 tools
from app.data.repositories import WalletRepo
from app.domain.types import SessionUser, Tier
from app.tools.get_payment_status import GetPaymentStatusIn, GetPaymentStatusOut
from app.tools.get_wallet_balance import GetWalletBalanceIn, GetWalletBalanceOut
from app.tools.registry import configure, registry, tool
from app.tools.request_limit_increase import RequestLimitIncreaseIn, RequestLimitIncreaseOut

# NOT `import app.tools.registry as registry_module`: app/tools/__init__.py
# does `from app.tools.registry import ..., registry, ...`, which rebinds the
# `app.tools` package's own `registry` *attribute* to the `Registry()`
# instance -- overwriting the submodule reference Python's import machinery
# set there a moment earlier. An attribute-path import (`import
# app.tools.registry as X`) resolves through that now-shadowed attribute and
# silently hands back the instance, not the module (confirmed the hard way:
# `AttributeError: 'Registry' object has no attribute '_REGISTRY'`).
# `importlib.import_module` looks the real module up in `sys.modules` by its
# full dotted name instead, sidestepping the shadowing entirely.
registry_module = importlib.import_module("app.tools.registry")


def test_get_wallet_balance_registered_with_correct_metadata() -> None:
    registered = registry.get("get_wallet_balance")
    assert registered is not None
    assert registered.tier == Tier.READ
    assert registered.latency_class == "read-single"
    assert registered.input_model is GetWalletBalanceIn
    assert registered.output_model is GetWalletBalanceOut


def test_get_payment_status_registered_with_correct_metadata() -> None:
    registered = registry.get("get_payment_status")
    assert registered is not None
    assert registered.tier == Tier.READ
    assert registered.latency_class == "read-single"
    assert registered.input_model is GetPaymentStatusIn
    assert registered.output_model is GetPaymentStatusOut


def test_request_limit_increase_registered_as_confirm_required() -> None:
    """The tier the confirm-gate state machine (docs/10 §4) hangs off
    of — gating is a property of the tier, not the handler (docs/10
    §8), so this assertion is the one regression guard that a future
    edit can't silently un-gate the mutation by picking the wrong
    `Tier`."""
    registered = registry.get("request_limit_increase")
    assert registered is not None
    assert registered.tier == Tier.CONFIRM_REQUIRED
    assert registered.latency_class == "write"
    assert registered.input_model is RequestLimitIncreaseIn
    assert registered.output_model is RequestLimitIncreaseOut


def test_unknown_tool_returns_none() -> None:
    """docs/10 §2's registry-lookup stage: an unknown name is a `None`
    result for the caller to turn into a `status=denied` audit row —
    never a `KeyError`."""
    assert registry.get("delete_everything") is None


def test_all_returns_exactly_the_three_phase2_tools() -> None:
    names = {registered.name for registered in registry.all()}
    assert names == {"get_wallet_balance", "get_payment_status", "request_limit_increase"}


def test_to_openai_tools_shape() -> None:
    tools = registry.to_openai_tools()
    assert len(tools) == 3

    by_name = {entry["function"]["name"]: entry for entry in tools}
    assert set(by_name) == {"get_wallet_balance", "get_payment_status", "request_limit_increase"}
    for entry in tools:
        assert entry["type"] == "function"
        assert entry["function"]["parameters"]["type"] == "object"

    # request_limit_increase's parameters must reflect its real schema,
    # not a placeholder -- both required fields present.
    limit_params = by_name["request_limit_increase"]["function"]["parameters"]
    assert set(limit_params["required"]) == {"current_limit", "requested_limit"}


def test_duplicate_registration_raises() -> None:
    """Guards the one way `@tool` itself could silently misbehave: two
    tools registered under the same name would let the second overwrite
    the first, and a caller resolving `registry.get(name)` would
    silently get the wrong handler."""

    with pytest.raises(ValueError):

        @tool(
            name="get_wallet_balance",
            tier=Tier.READ,
            latency_class="read-single",
            repo_type=WalletRepo,
            description="duplicate",
        )
        async def _dup(
            principal: SessionUser, args: GetWalletBalanceIn, repo: WalletRepo
        ) -> GetWalletBalanceOut:
            raise NotImplementedError


# --------------------------------------------------------------------------
# The `ToolHandler`-shape bridge closure itself — the part that opens a
# session, builds a repo, calls the raw handler, and commits. Review
# finding (HIGH): an earlier version of `configure`/`_wrapped` built its
# own independent engine via `create_engine_and_sessionmaker(get_settings())`,
# which meant NOTHING exercised this closure's actual behavior (every test
# above calls the raw 3-arg handler directly, never the registered 2-arg
# one). Making the sessionmaker injectable via `configure()` is what makes
# these tests possible at all, not just what fixes the duplicate-pool bug.
# --------------------------------------------------------------------------


class _FakeIn(BaseModel):
    pass


class _FakeOut(BaseModel):
    value: str


class _FakeRepo:
    def __init__(self, session: object) -> None:
        self.session = session


class _FakeSession:
    def __init__(self) -> None:
        self.commit_called = False
        self.entered = False

    async def __aenter__(self) -> _FakeSession:
        self.entered = True
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def commit(self) -> None:
        self.commit_called = True


class _FakeSessionmaker:
    """Stands in for `async_sessionmaker[AsyncSession]` — calling it
    (sync, matching the real thing) returns a fresh session-shaped async
    context manager, never a coroutine."""

    def __init__(self) -> None:
        self.sessions: list[_FakeSession] = []

    def __call__(self) -> _FakeSession:
        session = _FakeSession()
        self.sessions.append(session)
        return session


@pytest.fixture(autouse=True)
def _reset_registry_sessionmaker() -> None:
    """`registry_module._sessionmaker` is a module-level global `configure()`
    mutates — reset it before and after every test in this file so one
    test's `configure()` call can't leak into another's "unconfigured"
    assertion (or vice versa). Also snapshots/restores `_REGISTRY` itself:
    `test_configure_wires_the_bridge_closure_...` below registers a throwaway
    test tool into the same module-level dict every other test in this file
    reads via `registry.all()`/`registry.get()` — without this, that one
    extra entry would permanently leak into `test_all_returns_exactly_the_
    three_phase2_tools` and any test run after it."""
    registry_module._sessionmaker = None
    registry_snapshot = dict(registry_module._REGISTRY)
    yield
    registry_module._sessionmaker = None
    registry_module._REGISTRY.clear()
    registry_module._REGISTRY.update(registry_snapshot)


async def test_calling_a_registered_tool_before_configure_raises() -> None:
    """The whole point of the fix: no silent fallback to a self-built
    engine — calling a tool with no sessionmaker wired is a loud startup-
    wiring bug, not a per-call one."""
    registered = registry.get("get_wallet_balance")
    assert registered is not None
    principal = SessionUser(user_id="usr_rajesh01")

    with pytest.raises(RuntimeError, match="configure"):
        await registered.handler(principal, GetWalletBalanceIn())


async def test_configure_wires_the_bridge_closure_to_open_build_call_and_commit() -> None:
    """The full round trip through `_wrapped`, exercised for the first
    time: `configure()` with a fake sessionmaker, then a registered
    handler call actually opens a session, constructs `repo_type(session)`,
    calls the raw 3-arg handler with it, and commits — all through the
    injected fake, no real engine anywhere."""
    fake_sessionmaker = _FakeSessionmaker()
    configure(fake_sessionmaker)  # type: ignore[arg-type]
    received: dict[str, object] = {}

    @tool(
        name="_test_bridge_tool",
        tier=Tier.READ,
        latency_class="read-single",
        repo_type=_FakeRepo,
    )
    async def _handler(principal: SessionUser, args: _FakeIn, repo: _FakeRepo) -> _FakeOut:
        received["principal"] = principal
        received["repo"] = repo
        return _FakeOut(value="ok")

    registered = registry.get("_test_bridge_tool")
    assert registered is not None
    principal = SessionUser(user_id="usr_rajesh01")

    result = await registered.handler(principal, _FakeIn())

    assert isinstance(result, _FakeOut)
    assert result.value == "ok"
    assert received["principal"] is principal
    assert isinstance(received["repo"], _FakeRepo)
    assert len(fake_sessionmaker.sessions) == 1
    session = fake_sessionmaker.sessions[0]
    assert session.entered is True
    assert session.commit_called is True
    assert received["repo"].session is session  # the exact session that was opened and committed
