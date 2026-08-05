"""Unit tests for `SemanticRepo` — docs/09 §6.2's scoped retrieval query
(docs/04-backend-architecture.md §5).

No Docker on this machine, so these drive the repo against a
`spec=AsyncSession` mock, exactly as `test_repositories.py` does and for
the reasons its module docstring gives. That verifies what this repo
*builds* — the statement it compiles, the session settings it applies, the
objects it returns — but not what a real Postgres does with it. The
behaviour that genuinely needs a server (does the HNSW index actually
return the merchant's own rows under a scoped query?) lives in
`tests/models/test_semantic_repo_pg.py`, which is Docker-gated and runs in
CI on every PR.

Two things here are worth more than the usual unit test:

- `test_search_statement_carries_both_branches_of_the_scoping_predicate`
  and its siblings compile the real statement to PostgreSQL SQL, so the
  security invariant is checked against generated SQL rather than against
  a mock's call record.
- `test_semantic_repo_is_the_only_module_in_app_that_reaches_memory_chunks`
  is the structural half of the handoff — see its docstring for what it
  does and does not prove.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement, Select

from app.config import Settings
from app.data.repositories.semantic_repo import SemanticRepo, _to_similarity
from app.domain.types import EMBEDDING_DIM, MemoryKind, SessionUser
from app.models.orm import memory_scope
from tests.conftest import SettingsFactory

# backend/tests/data/repositories/test_semantic_repo.py -> backend/
BACKEND_ROOT = Path(__file__).resolve().parents[3]
APP_ROOT = BACKEND_ROOT / "app"

PRINCIPAL = SessionUser(user_id="usr_rajesh01")

# Uppercase on purpose: it is what separates raw SQL from the prose in
# `app/domain/types.py`'s validator messages, which name `memory_chunks`
# legitimately. Every SQL string in this codebase spells its keywords in
# caps (see `SemanticRepo._apply_hnsw_settings`, migrations/).
_SQL_VERBS = ("SELECT", "FROM", "INSERT", "UPDATE", "DELETE")


def _embedding(fill: float = 0.01) -> tuple[float, ...]:
    return tuple([fill] * EMBEDDING_DIM)


def _row(
    chunk_id: int,
    kind: str,
    source_id: str,
    content: str,
    user_id: str | None,
    distance: float,
) -> SimpleNamespace:
    """One result row in the shape `search()` reads it: the six selected
    columns as attributes, `distance` last."""
    return SimpleNamespace(
        id=chunk_id,
        kind=kind,
        source_id=source_id,
        content=content,
        user_id=user_id,
        distance=distance,
    )


def make_session(rows: list[SimpleNamespace] | None = None) -> AsyncMock:
    """A `spec=AsyncSession` mock scripted for `search()`'s three
    statements: the two `SET LOCAL`s, then the `SELECT` whose result is
    iterated. Returning a plain list for the SELECT is enough — `search()`
    iterates the result directly."""
    session = AsyncMock(spec=AsyncSession)
    session.execute.side_effect = [None, None, rows if rows is not None else []]
    return session


def executed_select(session: AsyncMock) -> Select[tuple[object, ...]]:
    """The `SELECT` statement object `search()` handed to the session."""
    statement = session.execute.await_args_list[-1].args[0]
    assert isinstance(statement, Select)
    return statement


def compiled(statement: Select[tuple[object, ...]]) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def bound_params(statement: Select[tuple[object, ...]]) -> dict[str, object]:
    return dict(statement.compile(dialect=postgresql.dialect()).params)


def compiled_clause(clause: ColumnElement[bool]) -> str:
    """Compile a boolean clause on its own, with bound values inlined.

    `literal_binds=True` matters: without it both sides render as
    `%(kind_1)s` placeholders and the comparison would pass for a scope
    naming the wrong merchant. With it, the principal's id is *in* the
    string being compared.
    """
    return str(clause.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


# --------------------------------------------------------------------------
# The scoping predicate, checked against generated SQL
# --------------------------------------------------------------------------


async def test_search_statement_carries_both_branches_of_the_scoping_predicate(
    settings: Settings,
) -> None:
    """**The security-invariant test at this layer.** docs/09 §6.2:
    `kind = 'kb_article' OR user_id = :session_user`. The KB branch alone
    makes a merchant's own summaries unreachable; the user branch alone
    hides the shared KB; dropping the whole clause makes every merchant's
    call summaries readable by every other merchant.

    Asserted on the compiled SQL and its bound parameters, not on a mock
    call, so it fails if the WHERE clause changes shape even when the
    Python still "calls memory_scope".

    **The equality assertion is the load-bearing one, and it is here
    because the presence checks below were not enough.** A security review
    demonstrated two mutations that survived the entire suite:

        .where(memory_scope(principal) | MemoryChunk.user_id.is_not(None))
        .where(memory_scope(principal) | (MemoryChunk.kind == "call_summary"))

    Both compile to a *superset* of the scope — `... OR user_id IS NOT
    NULL` makes every merchant's call summaries readable by every
    merchant — and both satisfy every "is this substring present"
    assertion, because widening a predicate adds disjuncts without
    removing any of the text being looked for. Presence checks can only
    catch *narrowing* and *dropping*, never widening. Comparing the whole
    `WHERE` clause to the composed predicate closes that class: a superset
    is not equal to the scope, so it fails here.

    The presence assertions are kept below the equality one, not replaced
    by it. They name the two branches individually, so a failure says
    which half went missing rather than only that two SQL strings differ.
    """
    session = make_session()

    await SemanticRepo(session, settings).search(_embedding(), PRINCIPAL, k=3)

    statement = executed_select(session)

    where = statement.whereclause
    assert where is not None
    assert compiled_clause(where) == compiled_clause(memory_scope(PRINCIPAL))

    sql = compiled(statement)
    assert "memory_chunks.kind = " in sql
    assert " OR " in sql
    assert "memory_chunks.user_id = " in sql
    assert "kb_article" in bound_params(statement).values()
    assert "usr_rajesh01" in bound_params(statement).values()


async def test_search_binds_the_passed_principal_not_a_captured_one(
    settings: Settings,
) -> None:
    """A scope that always names the same merchant is the cross-tenant bug
    in its most direct form."""
    session = make_session()

    await SemanticRepo(session, settings).search(
        _embedding(), SessionUser(user_id="usr_priya02"), k=3
    )

    values = bound_params(executed_select(session)).values()
    assert "usr_priya02" in values
    assert "usr_rajesh01" not in values


async def test_search_orders_by_cosine_distance_and_limits_in_sql(
    settings: Settings,
) -> None:
    """`<=>` is pgvector's cosine-distance operator — the metric the HNSW
    index's `vector_cosine_ops` opclass and the 0.70 floor are both
    expressed in. `<->` (L2) or `<#>` (inner product) here would still
    return rows, in a different order, and the floor would silently mean
    something else.

    The `LIMIT` is in SQL and it is the final `k`: the database applies it
    *after* the scope, so no foreign row is ever transported into this
    process to be filtered out here.
    """
    session = make_session()

    await SemanticRepo(session, settings).search(_embedding(), PRINCIPAL, k=3)

    statement = executed_select(session)
    sql = compiled(statement)
    assert "<=>" in sql
    assert "<->" not in sql
    assert "<#>" not in sql
    assert "ORDER BY distance" in sql
    assert "LIMIT" in sql
    assert 3 in bound_params(statement).values()


async def test_search_does_not_over_fetch_beyond_k(settings: Settings) -> None:
    """Explicitly pins the forbidden shortcut (`SemanticMemoryProto` handoff
    3): fetching a larger global top-k and filtering in Python pulls other
    merchants' call summaries into application memory, which is exactly the
    exposure the predicate prevents. The SQL LIMIT must be `k` itself, not
    a multiple of it.
    """
    session = make_session()

    await SemanticRepo(session, settings).search(_embedding(), PRINCIPAL, k=3)

    assert 3 in bound_params(executed_select(session)).values()
    assert executed_select(session)._limit == 3


# --------------------------------------------------------------------------
# HNSW session settings — the post-filter mitigation
# --------------------------------------------------------------------------


async def test_search_sets_the_hnsw_knobs_before_issuing_the_query(
    settings: Settings,
) -> None:
    """Both are `SET LOCAL`, so they must be in the same transaction as
    the SELECT and must precede it — a knob set afterwards affects
    nothing. Order is asserted, not just presence."""
    session = make_session()

    await SemanticRepo(session, settings).search(_embedding(), PRINCIPAL, k=3)

    statements = [str(call.args[0]) for call in session.execute.await_args_list]
    assert statements[0] == "SET LOCAL hnsw.ef_search = 100"
    assert statements[1] == "SET LOCAL hnsw.iterative_scan = strict_order"
    assert "SELECT" in statements[2]


async def test_iterative_scan_defaults_to_strict_order_not_relaxed(
    settings: Settings,
) -> None:
    """`relaxed_order` is documented to return rows slightly out of
    distance order. Inside a `LIMIT k` that lets a worse row displace a
    better one, silently changing which three chunks reach the prompt and
    which side of the 0.70 floor they land on."""
    assert settings.memory_hnsw_iterative_scan == "strict_order"


async def test_empty_iterative_scan_config_skips_the_statement_entirely(
    settings_factory: SettingsFactory,
) -> None:
    """The escape hatch for a pre-0.8.0 pgvector, where the parameter does
    not exist. It must skip the statement, not send `off` — sending any
    value at all is the thing that fails on such a server."""
    settings = settings_factory(memory_hnsw_iterative_scan="")
    session = AsyncMock(spec=AsyncSession)
    session.execute.side_effect = [None, []]

    await SemanticRepo(session, settings).search(_embedding(), PRINCIPAL, k=3)

    statements = [str(call.args[0]) for call in session.execute.await_args_list]
    assert len(statements) == 2
    assert "iterative_scan" not in statements[0]
    assert "SELECT" in statements[1]


@pytest.mark.parametrize("mode", ["off", "relaxed_order", "strict_order"])
async def test_search_accepts_every_pgvector_iterative_scan_mode(
    settings_factory: SettingsFactory, mode: str
) -> None:
    """pgvector's own enum is exactly these three values."""
    session = make_session()

    await SemanticRepo(session, settings_factory(memory_hnsw_iterative_scan=mode)).search(
        _embedding(), PRINCIPAL, k=3
    )

    assert str(session.execute.await_args_list[1].args[0]) == (
        f"SET LOCAL hnsw.iterative_scan = {mode}"
    )


async def test_search_rejects_an_unknown_iterative_scan_mode(
    settings_factory: SettingsFactory,
) -> None:
    """Both HNSW values are interpolated into SQL text (`SET LOCAL` takes
    no bind parameters), so they are validated against an allowlist first.
    Neither is user-influenced today; a config value reaching a SQL string
    unvalidated is the shape of the bug regardless."""
    settings = settings_factory(memory_hnsw_iterative_scan="strict_order; DROP TABLE merchants")
    session = make_session()

    with pytest.raises(ValueError, match="memory_hnsw_iterative_scan must be one of"):
        await SemanticRepo(session, settings).search(_embedding(), PRINCIPAL, k=3)

    # No statement containing the payload is ever sent — the allowlist runs
    # before interpolation, not after.
    sent = " ".join(str(call.args[0]) for call in session.execute.await_args_list)
    assert "DROP TABLE" not in sent


async def test_an_invalid_mode_executes_nothing_at_all(
    settings_factory: SettingsFactory,
) -> None:
    """Both HNSW values are validated before *either* is executed.

    Previously the checks were interleaved with the statements — validate
    `ef_search`, `SET` it, then validate `mode` — so an invalid mode raised
    with one `SET LOCAL` already sent. Harmless in practice (`SET LOCAL`
    dies with the transaction the caller rolls back) but it half-applied
    config that had not finished being validated, which is not what
    `_apply_hnsw_settings`'s docstring describes. This pins the ordering so
    the interleaving cannot come back.
    """
    settings = settings_factory(memory_hnsw_iterative_scan="nonsense")
    session = make_session()

    with pytest.raises(ValueError, match="memory_hnsw_iterative_scan must be one of"):
        await SemanticRepo(session, settings).search(_embedding(), PRINCIPAL, k=3)

    session.execute.assert_not_awaited()


@pytest.mark.parametrize("ef_search", [0, -1, 1001])
async def test_search_rejects_an_out_of_range_ef_search(
    settings_factory: SettingsFactory, ef_search: int
) -> None:
    """pgvector's range for `hnsw.ef_search` is 1..1000. Checked here so a
    bad config value names the field, instead of surfacing as a Postgres
    error in the middle of a transaction."""
    settings = settings_factory(memory_hnsw_ef_search=ef_search)
    session = make_session()

    with pytest.raises(ValueError, match="memory_hnsw_ef_search must be in 1..1000"):
        await SemanticRepo(session, settings).search(_embedding(), PRINCIPAL, k=3)


async def test_ef_search_default_is_above_pgvectors_own_default(settings: Settings) -> None:
    """pgvector defaults `hnsw.ef_search` to 40. Leaving it there is the
    documented cliff: at 40 candidates, a predicate matching 10% of rows
    returns ~4 rows on average, and a merchant whose summaries all fall
    outside those 40 gets none of their own back."""
    assert settings.memory_hnsw_ef_search > 40


# --------------------------------------------------------------------------
# Row mapping and the distance -> similarity conversion
# --------------------------------------------------------------------------


async def test_search_maps_rows_to_retrieved_memories_in_distance_order(
    settings: Settings,
) -> None:
    session = make_session(
        [
            _row(1, "kb_article", "kb_daily_limits", "Limits reset at midnight.", None, 0.05),
            _row(
                2, "call_summary", "sess_a1f3c9", "Payment declined.", "usr_rajesh01", 0.20
            ),
        ]
    )

    results = await SemanticRepo(session, settings).search(_embedding(), PRINCIPAL, k=3)

    assert [result.chunk_id for result in results] == [1, 2]
    assert results[0].kind is MemoryKind.KB_ARTICLE
    assert results[0].user_id is None
    assert results[1].kind is MemoryKind.CALL_SUMMARY
    assert results[1].user_id == "usr_rajesh01"
    assert results[1].source_id == "sess_a1f3c9"


async def test_search_converts_cosine_distance_to_cosine_similarity(
    settings: Settings,
) -> None:
    """pgvector's `<=>` returns cosine *distance* (lower is better);
    `RetrievedMemory.similarity` is cosine *similarity* (higher is
    better), which is what the 0.70 floor is expressed in. Returning the
    distance unconverted would invert the floor comparison and admit
    exactly the marginal results the floor exists to drop.
    """
    session = make_session(
        [_row(1, "kb_article", "kb_x", "...", None, 0.28)]
    )

    results = await SemanticRepo(session, settings).search(_embedding(), PRINCIPAL, k=3)

    assert results[0].similarity == pytest.approx(0.72)


def test_similarity_snaps_float_error_at_the_bounds_only() -> None:
    """A vector identical to the query can land a hair outside
    [-1.0, 1.0] through float subtraction alone, which would trip
    `RetrievedMemory`'s range validator on a perfectly good result. The
    snap covers that and nothing else: a value further out means the wrong
    operator was used (L2 distances exceed 1 routinely), and that is left
    to raise rather than silently clamped into a plausible-looking score.
    """
    assert _to_similarity(-1e-12) == 1.0
    assert _to_similarity(2.0 + 1e-12) == -1.0
    assert _to_similarity(0.3) == pytest.approx(0.7)
    # Well outside the epsilon: passed through, so RetrievedMemory rejects it.
    assert _to_similarity(2.5) == pytest.approx(-1.5)


async def test_search_rejects_a_row_whose_similarity_is_not_a_cosine(
    settings: Settings,
) -> None:
    """End-to-end consequence of the test above: a distance that cannot be
    a cosine distance reaches `RetrievedMemory`'s validator and raises,
    rather than being clamped into a score that looks fine."""
    session = make_session([_row(1, "kb_article", "kb_x", "...", None, 3.0)])

    with pytest.raises(ValueError, match="cosine similarity"):
        await SemanticRepo(session, settings).search(_embedding(), PRINCIPAL, k=3)


async def test_search_returns_empty_when_nothing_matches(settings: Settings) -> None:
    session = make_session([])

    assert await SemanticRepo(session, settings).search(_embedding(), PRINCIPAL, k=3) == []


# --------------------------------------------------------------------------
# Handoff 1/2: the predicate is composed, and this repo is the only reader
# --------------------------------------------------------------------------


def _module_sources() -> dict[Path, ast.Module]:
    return {
        path: ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(APP_ROOT.rglob("*.py"))
    }


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """`id()`s of the string-constant nodes that are docstrings, so a
    module that *describes* `memory_chunks` in prose is not mistaken for
    one that queries it. Comments never appear in the AST at all, so they
    need no handling."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ids.add(id(first.value))
    return ids


def _root_name(node: ast.expr) -> str | None:
    """The leftmost identifier of a dotted expression: `a.b.c` -> `"a"`."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def test_semantic_repo_is_the_only_module_in_app_that_reaches_memory_chunks() -> None:
    """**Handoff 2, the structural half.** `SemanticMemoryProto`'s
    docstring asks for "`SemanticRepo` as the sole query path... so an
    unscoped query has nowhere to live". Python cannot enforce that, so
    this test is the enforcement: any new module that imports the
    `memory_chunks` ORM class or names the table in SQL text fails the
    build, and the author has to justify a second reader.

    Allowlisted: `app/models/orm.py` declares the table and owns
    `memory_scope`; `app/data/repositories/semantic_repo.py` is the query
    path itself.

    **What this does not prove.** It is a lint over the source tree, not a
    proof about runtime. A module could reach the table through a
    dynamically built string, through raw asyncpg, or by constructing the
    attribute name at runtime (`getattr(orm, "Memory" + "Chunk")`). It
    closes the easy, likely path — someone writing a second
    `select(MemoryChunk)` because it is convenient — and nothing more.

    A security review measured which evasions actually work, and the
    earlier version of this list named the wrong ones. Aliasing (`from
    app.models.orm import MemoryChunk as MC`) was listed as a gap but is
    caught, because `ast.alias.name` holds the original name regardless of
    `as`. What genuinely slipped past was module-attribute access —
    `import app.models.orm` or `from app.models import orm`, then
    `orm.MemoryChunk` — which is now caught below by binding module
    aliases first and flagging attribute access through them. That two-step
    is why the check is not a blanket "any attribute named MemoryChunk":
    `app.domain.types` exports an unrelated Pydantic model of the same
    name, and flagging `types.MemoryChunk` would be a false positive.

    Two deliberate non-matches:
    `app.domain.types.MemoryChunk` (the unrelated write-side Pydantic
    model of the same name) is ignored because only imports from
    `app.models*` count; and a string merely *naming* the table is ignored
    unless it also carries a SQL verb, because `app/domain/types.py`'s
    validator messages legitimately say "memory_chunks.user_id is
    required..." in prose. That second rule is the scan's real soft spot —
    lowercase raw SQL would slip past it.
    """
    allowed = {
        APP_ROOT / "models" / "orm.py",
        APP_ROOT / "data" / "repositories" / "semantic_repo.py",
    }
    offenders: dict[str, list[str]] = {}

    for path, tree in _module_sources().items():
        if path in allowed:
            continue
        reasons: list[str] = []
        docstrings = _docstring_nodes(tree)

        # Local names bound to an `app.models` *module* (rather than to a
        # name inside one): `import app.models.orm as m`, plain `import
        # app.models.orm` (which binds `app`), and `from app.models import
        # orm`. Attribute access through any of these is the evasion the
        # name-based check below cannot see.
        module_aliases: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("app.models"):
                        module_aliases.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and (node.module or "") == "app.models":
                for alias in node.names:
                    module_aliases.add(alias.asname or alias.name)

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("app.models"):
                # `alias.name` is the original name even under `as`, so
                # `import MemoryChunk as MC` is caught here.
                names = {alias.name for alias in node.names}
                leaked = names & {"MemoryChunk", "memory_scope"}
                if leaked:
                    reasons.append(f"imports {sorted(leaked)} from {node.module}")
            if (
                isinstance(node, ast.Attribute)
                and node.attr in {"MemoryChunk", "memory_scope"}
                and isinstance(node.value, ast.Name | ast.Attribute)
                and _root_name(node.value) in module_aliases
            ):
                reasons.append(f"reaches {node.attr} through a module alias on line {node.lineno}")
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "memory_chunks" in node.value
                and any(verb in node.value for verb in _SQL_VERBS)
                and id(node) not in docstrings
            ):
                reasons.append(f"names the table in SQL text on line {node.lineno}")
        if reasons:
            offenders[str(path.relative_to(BACKEND_ROOT))] = reasons

    assert offenders == {}, (
        "memory_chunks must be reachable only through SemanticRepo.search(), which is "
        f"the one function that applies docs/09 §6.2's scoping predicate. Found: {offenders}"
    )


def test_search_composes_memory_scope_rather_than_rebuilding_the_or() -> None:
    """**Handoff 1.** `memory_scope()` exists so the predicate has one
    reviewable call site; Batch 1's note is explicit that Batch 2 should
    "compose it; do not re-derive it". A hand-written OR would still pass
    every SQL assertion above while quietly forking the definition, so
    this asserts the call itself.
    """
    source = (APP_ROOT / "data" / "repositories" / "semantic_repo.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert "memory_scope" in called

    # And no locally rebuilt equivalent. Executable string constants only —
    # the module's own docstring quotes docs/09 §6.2's predicate verbatim,
    # which is documentation, not a second definition.
    docstrings = _docstring_nodes(tree)
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    }

    assert not literals & {"kb_article", "call_summary"}, (
        "the kind literals belong to MemoryKind and to memory_scope(); spelling one "
        "here is how a second, drifting copy of the scoping predicate starts"
    )


# --------------------------------------------------------------------------
# Handoff 3 (repo half): no parameter here can widen the scope either
# --------------------------------------------------------------------------


def test_search_exposes_no_parameter_that_could_widen_scope() -> None:
    """The conformance check on `SemanticMemory.retrieve` (see
    tests/memory/test_semantic_memory.py) would be worth little if the
    escape hatch could simply be added one layer down, on the method that
    actually issues the SQL. Same freeze, applied here."""
    sig = inspect.signature(SemanticRepo.search, eval_str=True)

    assert list(sig.parameters) == ["self", "embedding", "principal", "k"]
    principal = sig.parameters["principal"]
    assert principal.default is inspect.Parameter.empty
    assert principal.annotation is SessionUser
    # k has no default here on purpose: SemanticMemory owns docs/09 §6.2's
    # top-3 default, so the repo cannot drift a second copy of it.
    assert sig.parameters["k"].default is inspect.Parameter.empty
    assert sig.parameters["k"].kind is inspect.Parameter.KEYWORD_ONLY


def test_semantic_repo_exposes_no_other_query_method() -> None:
    """`search()` is the only read path. A new public method here is a new
    place for an unscoped query to live, so adding one is a deliberate act
    that fails this test first.

    **This is an allowlist, not a bare `== {"search"}`, and the difference
    is not cosmetic.** The equality form asserted that `vars()` held
    exactly `search` — which meant *overriding* an inherited method put a
    second name in `vars()` and failed the test. A security review hit
    this directly: `get()` is inherited as `session.get(MemoryChunk, id)`,
    an unscoped cross-tenant read, and the natural hardening — override it
    to raise — was blocked by the very test meant to keep unscoped queries
    out. A guard that forbids the fix for the hole it is guarding is worse
    than no guard. The allowlist names what each entry is for, so closing
    an inherited hole is a one-line addition here with a reason attached,
    while a genuinely new query method still fails.

    `add`/`update` stay inherited and unlisted: they are write paths, not
    reads, and `SemanticMemoryProto` assigns indexing to this repo, so a
    later batch is expected to override them.
    """
    public = {
        name
        for name, value in vars(SemanticRepo).items()
        if not name.startswith("_") and inspect.isfunction(value)
    }

    allowed = {
        # The scoped read path — the whole point of the class.
        "search",
        # Not a query: an override that refuses the inherited unscoped
        # primary-key fetch. See SemanticRepo.get()'s docstring.
        "get",
    }
    assert public == allowed


async def test_get_refuses_rather_than_reading_across_merchants(
    settings: Settings,
) -> None:
    """The inherited `get()` would issue `session.get(MemoryChunk, id)` —
    no principal, no `memory_scope`, so a `call_summary` belonging to any
    merchant comes back to whoever asks. This asserts the override is in
    force *and* that it never reaches the session, so the refusal is not
    merely a post-hoc error after the row was already fetched.
    """
    session = make_session()

    with pytest.raises(NotImplementedError, match="memory_scope"):
        await SemanticRepo(session, settings).get("7")

    session.get.assert_not_called()
    session.execute.assert_not_called()
