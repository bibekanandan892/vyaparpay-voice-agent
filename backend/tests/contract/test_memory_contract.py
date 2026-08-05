"""Contract-freeze tests for the Phase-5 Batch-1 memory subsystem.

Everything here runs in the standard suite — no Docker, no Postgres. It
covers the two halves of the freeze that a live database would *not* catch
anyway:

1. **Protocol signature freeze** (`EmbeddingProvider`, `SemanticMemoryProto`).
   The point of pinning these is that Batches 2-4 build against them in
   parallel; a signature that drifts silently is the whole failure mode
   the freeze exists to prevent. `inspect.signature` is the mechanism
   rather than a mypy assertion because the repo's type gate is
   `mypy app` — it never type-checks `tests/`, so a mypy-only conformance
   check would be enforced by nothing.

2. **Schema shape compiled from the ORM metadata.** `tests/models/` needs
   a live Postgres and is excluded from the standard run, which would
   leave the four new tables' constraints with no coverage that actually
   executes on this machine. Compiling `CreateTable`/`CreateIndex` against
   the postgresql dialect proves the DDL the models *declare* — the CHECK
   is present, the vector column is 1536-wide, the index is HNSW/cosine —
   without a server. It cannot prove Postgres enforces them; that is
   `tests/models/test_memory_orm.py`'s job, and it is Docker-gated.

The security invariant under test in part 1 is docs/09 §6.2's retrieval
scoping (`kind = 'kb_article' OR user_id = :session_user`), encoded as a
required `principal` parameter. These tests guard the *encoding*: that
`principal` stays required and that no parameter is added which could
widen the scope. They do not and cannot prove any implementation honors
it — see `SemanticMemoryProto`'s docstring, which says so explicitly.
"""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.domain.interfaces import EmbeddingProvider, SemanticMemoryProto
from app.domain.types import (
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
    RETRIEVAL_FLOOR,
    RETRIEVAL_TOP_K,
)
from app.models.orm import ConversationSummary, KbArticle, MemoryChunk, UserProfile

# backend/tests/contract/test_memory_contract.py -> backend/
BACKEND_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_0002 = BACKEND_ROOT / "migrations" / "versions" / "0002_memory_subsystem.py"


def _signature(func: object) -> inspect.Signature:
    """`inspect.signature` with string annotations resolved — every module
    in app/domain/ uses `from __future__ import annotations`."""
    return inspect.signature(func, eval_str=True)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Embedding contract constants (docs/09 §6.1, docs/16 ADR-003)
# --------------------------------------------------------------------------


def test_embedding_contract_constants_are_frozen() -> None:
    """1536-dim `text-embedding-3-small`. These are not tunables: every
    stored vector and every query vector must come from the same model, or
    cosine distance between them is meaningless rather than merely worse.
    Changing either is a re-embed of the whole derived table."""
    assert EMBEDDING_DIM == 1536
    assert EMBEDDING_MODEL == "text-embedding-3-small"


def test_retrieval_defaults_match_docs_09_section_6_2() -> None:
    """Top-3 by cosine similarity, floor 0.70 (docs/09 §6.2)."""
    assert RETRIEVAL_TOP_K == 3
    assert RETRIEVAL_FLOOR == pytest.approx(0.70)


# --------------------------------------------------------------------------
# EmbeddingProvider — batched, one call, positionally aligned
# --------------------------------------------------------------------------


def test_embedding_provider_takes_a_batch_not_a_single_string() -> None:
    """`embed(texts: list[str]) -> list[Embedding]`. The seed embedder
    pushes ~180 KB chunks through this in one pass; a one-text-per-call
    shape would make that ~180 sequential HTTP round trips."""
    sig = _signature(EmbeddingProvider.embed)

    assert list(sig.parameters) == ["self", "texts"]
    texts = sig.parameters["texts"]
    assert texts.default is inspect.Parameter.empty
    assert texts.annotation == list[str]
    assert sig.return_annotation == list[tuple[float, ...]]


def test_embedding_provider_embed_is_async() -> None:
    assert inspect.iscoroutinefunction(EmbeddingProvider.embed)


# --------------------------------------------------------------------------
# SemanticMemoryProto — the scoping invariant, encoded in the signature
# --------------------------------------------------------------------------


def test_retrieve_requires_a_principal() -> None:
    """**The security-invariant test.** docs/09 §6.2 scopes retrieval to
    `kind = 'kb_article' OR user_id = :session_user`; docs/14 treats
    cross-tenant reach by an authenticated user as a top-row threat. The
    encoding is that `principal` is a required parameter, so a caller
    cannot issue an unscoped query by omission — mypy rejects the missing
    argument and CPython raises TypeError at the call.

    If someone later gives it a default (`SessionUser | None = None`), the
    scoping predicate has to degrade to something that still returns rows
    for a `None` scope — in practice the `user_id` clause gets dropped and
    every merchant's call summaries become readable by every other
    merchant. This assertion is what fails first if that happens.
    """
    from app.domain.types import SessionUser

    principal = _signature(SemanticMemoryProto.retrieve).parameters["principal"]

    assert principal.default is inspect.Parameter.empty, (
        "principal must stay required — a default is what turns a forgotten "
        "argument into a cross-tenant read (docs/09 §6.2, docs/14)"
    )
    assert principal.annotation is SessionUser, (
        "principal must be the same SessionUser the ToolExecutor injects "
        "(docs/10 §1 invariant 3) — not a bare user_id string a caller "
        "could assemble from untrusted input"
    )


def test_retrieve_exposes_no_parameter_that_could_widen_scope() -> None:
    """Freeze the whole parameter set, not just `principal`.

    A required `principal` is not sufficient on its own: an added
    `include_all_users: bool = False`, a `user_id: str | None = None`
    override, or a `kind` filter that accepts "any" would each reintroduce
    the unscoped query the required parameter was meant to make
    unreachable. Pinning the exact set means such a parameter cannot be
    added without this test failing and forcing the discussion.

    Signature is docs/05 §3.7's verbatim: `retrieve(query, principal, *,
    k=3, floor=0.70)`.
    """
    sig = _signature(SemanticMemoryProto.retrieve)

    assert list(sig.parameters) == ["self", "query", "principal", "k", "floor"]
    assert sig.parameters["query"].default is inspect.Parameter.empty
    # Asserted against docs/09 §6.2's literals, not against RETRIEVAL_TOP_K /
    # RETRIEVAL_FLOOR: the protocol's defaults *are* those constants, so
    # comparing the two would be a tautology that no change could ever fail
    # (a mutation sweep caught exactly that). The literals make both this
    # test and test_retrieval_defaults_match_docs_09_section_6_2 meaningful,
    # and catch a default hardcoded past the constant.
    assert sig.parameters["k"].default == 3
    assert sig.parameters["floor"].default == pytest.approx(0.70)
    # k/floor are keyword-only per docs/05 §3.7's `*`.
    assert sig.parameters["k"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["floor"].kind is inspect.Parameter.KEYWORD_ONLY


def test_retrieve_returns_scored_results() -> None:
    """docs/05 §3.7 writes `-> list[Chunk]` but defines no `Chunk`; the
    floor/top-k rule needs a similarity score to compare, so
    `RetrievedMemory` pins the shape (see its contract note)."""
    from app.domain.types import RetrievedMemory

    sig = _signature(SemanticMemoryProto.retrieve)

    assert sig.return_annotation == list[RetrievedMemory]
    assert inspect.iscoroutinefunction(SemanticMemoryProto.retrieve)


# --------------------------------------------------------------------------
# Schema shape, compiled from ORM metadata (no server needed)
# --------------------------------------------------------------------------


def _table_ddl(model: type) -> str:
    return str(CreateTable(model.__table__).compile(dialect=postgresql.dialect()))  # type: ignore[attr-defined]


def test_memory_chunks_embedding_column_is_1536_wide() -> None:
    ddl = _table_ddl(MemoryChunk)

    assert "embedding VECTOR(1536) NOT NULL" in ddl


def test_memory_chunks_declares_the_user_scope_check() -> None:
    """`ck_memory_chunks_user_scope` — the constraint that makes both docs'
    "NULL for KB; REQUIRED for call_summary (scoping)" annotation true.
    Neither docs/09 §6.1 nor docs/12 §5 declares it; the annotation was
    unenforced until this model added it.

    A `call_summary` with a NULL `user_id` matches no merchant at all
    (`NULL = 'usr_x'` is NULL, not true), so it is unreachable by its own
    owner; a `kb_article` carrying a `user_id` is the mirror-image bug.
    """
    ddl = _table_ddl(MemoryChunk)

    assert "CHECK ((kind = 'call_summary') = (user_id IS NOT NULL))" in ddl


def test_memory_chunks_declares_the_kind_check() -> None:
    ddl = _table_ddl(MemoryChunk)

    assert "CHECK (kind IN ('kb_article', 'call_summary'))" in ddl


def test_memory_chunks_index_is_hnsw_cosine() -> None:
    """HNSW over ivfflat (docs/12 §5): ivfflat needs training and list
    retuning as the corpus grows; HNSW builds incrementally. `cosine`
    because that is the metric the 0.70 floor is expressed in — an L2 or
    inner-product opclass here would silently make the floor mean
    something else."""
    index = next(i for i in MemoryChunk.__table__.indexes if i.name == "idx_chunks_embedding")
    ddl = str(CreateIndex(index).compile(dialect=postgresql.dialect()))

    assert "USING hnsw" in ddl
    assert "vector_cosine_ops" in ddl


def test_source_of_record_tables_carry_no_embedding_column() -> None:
    """docs/12 §4.3/§5: summaries and articles deliberately have no
    embedding. Vectors live only in the derived `memory_chunks`, so
    re-embedding (model swap, dimension change) rebuilds one table instead
    of `ALTER`-ing every table that owns text — and article-level vectors
    would average away exactly the section a query wanted."""
    assert "embedding" not in ConversationSummary.__table__.columns
    assert "embedding" not in KbArticle.__table__.columns


def test_conversation_summaries_is_keyed_and_indexed_for_retrieval_and_deletion() -> None:
    """PK on session_id makes the post-call pipeline idempotent (docs/09
    §8); `idx_summaries_user` serves both the RAG-side read and
    right-to-delete's `DELETE WHERE user_id = ?` (docs/09 §10)."""
    table = ConversationSummary.__table__

    assert [c.name for c in table.primary_key.columns] == ["session_id"]
    assert not table.columns["user_id"].nullable
    assert any(i.name == "idx_summaries_user" for i in table.indexes)


def test_user_profiles_jsonb_columns_default_to_empty_containers() -> None:
    """docs/09 §5.1: `facts`/`preferences` default `'{}'`, `open_issues`
    defaults `'[]'` — a profile with no data reads as empty, never NULL,
    so the profile-slot renderer never has to distinguish the two."""
    table = UserProfile.__table__

    for column, default in (("facts", "'{}'"), ("preferences", "'{}'"), ("open_issues", "'[]'")):
        assert not table.columns[column].nullable
        assert table.columns[column].server_default.arg.text == default  # type: ignore[union-attr]


def test_user_profiles_has_no_foreign_key_to_merchants() -> None:
    """docs/09 §5.1 (the owning doc, canon §11) declares a bare `user_id
    TEXT PRIMARY KEY`. docs/12 §1's ER diagram draws
    `merchants ||--|| user_profiles` as though the relation were enforced;
    it is not, in either doc's DDL, and this model does not invent it.

    This test pins the *reported discrepancy*, so that adding the FK later
    is a deliberate schema decision with a migration, not a silent
    reconciliation of two docs that disagree.
    """
    assert UserProfile.__table__.foreign_keys == set()


# --------------------------------------------------------------------------
# Migration 0002 — the pgvector-extension rule
# --------------------------------------------------------------------------


def _load_migration_0002() -> ModuleType:
    """Load the revision module by path: `0002_memory_subsystem` starts
    with a digit, so it is not importable as a dotted name (Alembic loads
    its revisions the same way, by file)."""
    spec = importlib.util.spec_from_file_location("_mig_0002", MIGRATION_0002)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0002_chains_onto_0001() -> None:
    mig = _load_migration_0002()

    assert mig.revision == "0002"
    assert mig.down_revision == "0001"


def test_migration_0002_never_touches_the_pgvector_extension() -> None:
    """0001 already runs `CREATE EXTENSION IF NOT EXISTS vector`, and the
    Postgres image is `pgvector/pgvector:pg16` — chosen in Phase 2 so
    Phase 5 needs no base-image migration (docs/12 §10, docs/16 ADR-003).
    0002 must neither recreate it nor drop it on downgrade: it is a
    shared, database-wide object, and dropping it would also break 0001's
    post-downgrade state, which assumes it present.

    Asserted against the source text because the statement would be an
    `op.execute` string that no metadata reflection would expose.
    """
    source = MIGRATION_0002.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    ).split('"""')[-1]

    assert "CREATE EXTENSION" not in code.upper()
    assert "DROP EXTENSION" not in code.upper()
