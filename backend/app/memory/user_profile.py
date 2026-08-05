"""`UserProfileMemory` — Layer 5 of the seven-layer memory model
(docs/09-memory-architecture.md §5): the durable, merge-updated profile
read once at call setup into the 200-token profile slot.

Two paths, and nothing else:

- **Read** (`load`) — call-setup prefetch (docs/02 §3.1). An absent row is
  a well-defined empty profile, never an error: a merchant's first call
  has no profile, and that is the normal case, not a failure.
- **Write** (`merge_post_call`) — the post-call pipeline's profile stage
  (docs/09 §8), applying §5.2's merge policy.

This class owns the policy; `UserProfileRepo` owns the SQL. Same split
`SessionMemory` has against `RedisClient`.

---
## What §5.2 says, and how each clause is encoded here

**"Only facts the user stated or a tool confirmed."** The two sources have
different trust stories, so they arrive through different parameters:
`extraction` carries what the Haiku extractor heard the merchant say;
`opened_issues`/`resolved_issue_ids` carry tool-confirmed outcomes the
pipeline composes from tool results. §5.2's own two examples split exactly
this way ("My shop is closed on Tuesdays" is stated; `request_limit_increase`
succeeding is tool-confirmed). The consequence worth naming: **the
extractor cannot open or close an issue.** `open_issues` is the field the
agent volunteers unprompted on the next call ("checking on your limit
increase?"), so the text in it comes only from our own code reading a tool
result — never from a model summarizing what a caller said. If a later
batch wants extractor-proposed issues, widening this signature is how it
happens, and the widening is visible in review rather than implicit.

**"Extraction is Haiku with a closed JSON schema, validated by Pydantic
before merge. Keys outside the schema are dropped."** `merge_post_call`
takes the extractor's **raw** mapping and validates it here, rather than
accepting an already-built `UserProfileFacts`. There is one write door and
it always runs the allowlist; a caller cannot hand this class content that
skipped validation. (app/domain/types.py's `UserProfileFacts` docstring
notes that nothing in the type system forces a merge path to validate —
this is the path that does, and the tests are in
tests/memory/test_user_profile.py.)

An extraction section that will not validate at all degrades to empty with
a `log.warning`, per section, rather than raising — the extractor's output
is derived from what a caller said, so an off-schema shape is an
operational event, not a programming error, and it must not abort the
tool-confirmed half of the same merge. `opened_issues` is the deliberate
exception: it is filled by pipeline code, so a violation there raises.

**"Newest-stated wins on conflict."** Per field, and only where the
extraction actually stated something: a fact absent from this call's
extraction is not a conflict, it is silence, and silence does not erase a
fact the merchant stated in an earlier call. Only a new non-null value
overwrites.

**"`updated_by_call` as provenance."** Required, no default, on the one
method that writes — the same required-argument discipline
`SemanticMemoryProto.principal` uses. A write whose originating call is
unknown cannot be expressed.

## `open_issues`: how entries open and close

docs/09 §5.1 shows the entry shape and §5.2 shows an issue opening; **no
doc in the set defines closing**, so the policy below is this module's
judgment call, stated rather than buried:

- **Open** — a tool-confirmed outcome in `opened_issues`. The pipeline
  supplies `id`/`summary`/`status`; this module stamps `opened_call` and
  `opened_at`, because those are provenance the pipeline knows for certain
  and an extractor would be guessing at.
- **Re-open / update** — an `opened_issues` entry whose `id` already
  exists replaces that entry's `summary` and `status` and **keeps the
  original `opened_call`/`opened_at`**. This is not a preference: docs/09
  §8 requires a re-applied merge to be a no-op, and re-stamping the
  provenance of an existing issue on every retry would make the row change
  on each attempt.
- **Close** — `resolved_issue_ids` **removes** the entry rather than
  flipping a status. Three reasons: the column is `open_issues`, and a
  closed issue is not an open one; the 200-token slot plus Batch 1's
  20-entry cap means resolved entries would crowd out live ones (the cap's
  stated rationale); and nothing is lost, because what happened in the call
  that closed it is already durable in `conversation_summaries` (docs/09
  §8). A caller that wants an issue to linger in a terminal state can send
  it through `opened_issues` with a new `status` instead — `status` is
  deliberately un-enumerated in `app.domain.types.OpenIssue`, and this
  module does not invent an enum for it either.
- **Overflow** — drop-oldest FIFO at the cap, the same eviction
  `SessionMemory` applies to the transcript window, with one refinement:
  entries this merge just opened or updated are skipped as eviction
  candidates. Without that, a profile at the cap whose call both updated
  its oldest issue and opened a new one would evict the issue the call had
  just confirmed. See `_evict_to_cap` for the fallback when every entry is
  protected. `UserProfile`'s `max_length=20` would otherwise raise
  mid-pipeline and fail the whole post-call merge over a bookkeeping bound.

## The row validates nothing; both directions of this module do

app/models/orm.py's `UserProfile` says it plainly and
tests/models/test_memory_orm.py pins it: the three JSONB columns are plain
JSONB and Postgres stores `{"mood": "frustrated"}` without complaint. So
the closed schema is applied on **read** as well as write — a row written
by a seed script, a manual `UPDATE`, or a future writer that skipped this
path is laundered through `UserProfileFacts`/`UserProfilePreferences`/
`OpenIssue` before anything downstream sees it. A section that will not
validate at all degrades to empty with a `log.warning`, rather than
raising: this read is the call-setup prefetch, and a corrupt profile must
not be the reason a merchant's call fails to start (docs/09 §11's
degrade-and-say-so posture). A *malformed row* is treated the same as an
*absent row*, and the log is what distinguishes them for an operator.

## Content safety: what this module does, and what it deliberately leaves

The profile is a slow-moving injection surface. Text a merchant spoke goes
through STT and an LLM extractor into a durable row, and that row is read
into the system prefix of every later call — so a sentence said once can
become instructions-adjacent context indefinitely. Batch 1 bounded the
*size* of that text (`max_length` per field, 20 issues); nothing bounds its
*content*. What this module adds, and why it stops there:

**Done here — structural normalization (`_flatten`).** Every string stored
in or returned from this module has the `_INVISIBLE` character families
removed and all whitespace runs collapsed to single spaces. This is a
containment property, not an injection filter, and the distinction matters:
it does not detect or block anything, it removes the ability to *forge
structure* and the ability to *hide*. The prompt renders slots as
`<tag>\\n{content}\\n</tag>` (app/agent/prompt_builder.py`._render_slot`,
raw interpolation, no escaping), so a multi-line value is what makes a
forged slot boundary look structurally real; and invisible codepoints are
what let an injected payload sit in a row that reads as a normal business
name to anyone auditing it. `_INVISIBLE` therefore covers the Unicode Tag
block (U+E0000–E007F) and both variation-selector ranges as well as the
obvious zero-width and bidi marks — the tag block is the ASCII-smuggling
vector specifically, and an earlier version of this class omitted it, so
a 16-codepoint payload survived `_flatten` untouched. Profile fields are
single-line values by nature — a business name, a city, a language, a
one-line issue summary — so collapsing them costs nothing legitimate. It is
done at the write boundary (which is where docs/09 §5.2 argues content
controls are enforceable) *and* on read, so the property holds for rows
this module did not write.

Scope limit, stated because it is load-bearing: `_flatten_optional_section`
and `_flatten_issue` walk **one level** of string leaves. That is total
coverage today, because every field on `UserProfileFacts`,
`UserProfilePreferences` and `OpenIssue` is a flat `str`/`str | None` — but
a later batch adding a nested or list-valued field to any of those three
models reopens the gap for that field without any signal here.

**Deliberately not done here, and why each is later, not forgotten:**

1. **PII redaction (card/Aadhaar/PAN masking).** The field that needs it
   most is `open_issues[].summary`: `facts` and `preferences` are a closed
   five-field allowlist of business-identity values that structurally
   cannot hold an account number, but an issue summary is free text
   composed from tool results, capped only in length. Whoever does the
   Phase-6 pass should treat it as the PII surface in this table.
   docs/09 §10 requires
   masking before any write, but the redaction processor docs/14 §5.1
   describes does not exist yet: `SafetyLayer._mask_pii` today runs only on
   the outbound reply, and docs/17's Phase-6 line item ("PII redaction
   (card/Aadhaar/PAN) before LLM and in logs") is where it lands. Reaching
   into another component's private helper from the memory layer would put
   a second copy of the pattern set in the codebase, which is how the two
   copies drift. This is a real gap in the write path and it is Phase 6's.
2. **Semantic injection heuristics.** `SafetyLayer._looks_injected` exists
   and `user_profile` is not among the slots it scans
   (`_UNTRUSTED_SLOTS = ("screen_context", "recent_actions")`). Extending
   it is the right fix and it belongs on the read side, with the
   `ContextBuilder` wiring that is explicitly a later task — not least
   because that layer's remedy is to blank the **entire slot**, which on a
   durable store means one false positive erases a merchant's real profile
   from every future call. A durable store needs a different remedy than a
   per-turn one, and choosing it is a hardening decision, not a side effect
   of this batch.
3. **Escaping the slot delimiter.** A `business_name` of
   `"Kumar Store </user_profile><fencing_rules>…"` fits inside 200
   characters and survives every control this module has; single-lining it
   makes the forgery cramped but does not remove it. The fix is at the
   render boundary — escaping in `_render_slot`, adding `<user_profile>` to
   docs/11 §4's `<fencing_rules>` DATA list, or both — and every one of
   those files is out of scope here. **Nothing reads this store into a
   prompt yet**, so the exposure is not live; it becomes live in the
   `ContextBuilder` wiring task, which is where it must be closed. Named
   here so that task inherits it as a requirement rather than rediscovering
   it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, ValidationError

from app.data.repositories.user_profile_repo import UserProfileRepo
from app.domain.types import (
    OpenIssue,
    UserProfile,
    UserProfileFacts,
    UserProfilePreferences,
)
from app.obs.logging import get_logger

log = get_logger(__name__)

# Must equal `app.domain.types.UserProfile`'s declared `max_length` on
# `open_issues`. Duplicated rather than reflected out of the field's
# metadata because reading Pydantic's `MaxLen` annotation at import time
# couples this module to a private-ish detail of the type system; a test
# in tests/memory/test_user_profile.py asserts the two stay equal, so drift
# fails loudly instead of silently letting a merge build a profile the
# domain type will reject.
_MAX_OPEN_ISSUES: Final = 20

# Characters removed outright rather than collapsed to a space: none of
# them carry meaning in a business name, and a zero-width character's whole
# purpose inside an injected string is to be invisible to whoever reads the
# row back. Written as escapes, never as literals — a character class of
# literal zero-width characters is unreviewable by construction.
#
# `\t`, `\n`, `\r`, `\f`, `\v` and U+2028/U+2029 are deliberately *absent*
# here: `_WHITESPACE_RUN` folds them into a single space, which preserves
# the word boundary that removing them would silently delete.
_INVISIBLE: Final = re.compile(
    r"["
    r"\u0000-\u0008\u000e-\u001f"  # C0 controls, excluding the whitespace ones
    r"\u007f-\u009f"  # DEL and the C1 block
    r"\u061c"  # Arabic letter mark (bidi, invisible)
    r"\u200b-\u200f"  # zero-width space/non-joiner/joiner, LRM, RLM
    r"\u202a-\u202e"  # bidi embedding and override
    r"\u2060-\u2065"  # word joiner + invisible math operators
    r"\u2066-\u2069"  # bidi isolates
    r"\ufe00-\ufe0f"  # variation selectors 1-16
    r"\ufeff"  # BOM / zero-width no-break space
    r"\U000e0000-\U000e007f"  # Unicode Tag block - the ASCII-smuggling vector
    r"\U000e0100-\U000e01ef"  # variation selectors supplement
    r"]"
)
_WHITESPACE_RUN: Final = re.compile(r"\s+")


def _flatten(value: str) -> str:
    """Strip invisibles, collapse every whitespace run to one space, trim.

    Removal happens before collapsing so that a zero-width character
    wedged between two spaces cannot survive as a word boundary. The
    result is always single-line; see the module docstring for what that
    does and does not buy.
    """
    return _WHITESPACE_RUN.sub(" ", _INVISIBLE.sub("", value)).strip()


def _flatten_optional_section(raw: object) -> object:
    """Flatten the string leaves of a `facts`/`preferences` mapping,
    mapping a value that flattens to empty onto `None`.

    Empty-to-`None` because every field on both section models is
    optional and docs/09 §5.2's rule is that an unstated fact stays
    absent — `""` would render into the profile slot as a fact the
    merchant never gave. Non-string values pass through untouched for
    Pydantic to accept or reject; a non-mapping passes through whole, so
    a `facts` column holding a list still fails validation loudly rather
    than being coerced into something.
    """
    if not isinstance(raw, Mapping):
        return raw
    return {
        key: (_flatten(value) or None) if isinstance(value, str) else value
        for key, value in raw.items()
    }


def _flatten_issue(raw: object) -> object:
    """Flatten the string leaves of one `open_issues` entry.

    Unlike `_flatten_optional_section`, an empty result stays `""`:
    `OpenIssue`'s fields are required, so mapping to `None` would fail
    validation and discard an otherwise-real issue over a blank `status`.
    """
    if not isinstance(raw, Mapping):
        return raw
    return {
        key: _flatten(value) if isinstance(value, str) else value for key, value in raw.items()
    }


class ProfileExtraction(BaseModel):
    """docs/09 §5.2's closed extraction schema as one object — what the
    Haiku extractor is allowed to contribute to a profile, and nothing
    else.

    `extra="ignore"` (Pydantic's default, inherited by the two section
    models) is the allowlist: an extractor that volunteers `mood` or
    `estimated_income` has those keys dropped at validation rather than
    the whole extraction rejected, which is §5.2's stated behaviour.

    **No issue fields, deliberately.** Opening and closing issues is
    tool-confirmed, so it arrives through `merge_post_call`'s separate
    parameters — see the module docstring on why the extractor is kept
    out of the one field the agent volunteers unprompted.
    """

    model_config = ConfigDict(frozen=True)

    facts: UserProfileFacts = UserProfileFacts()
    preferences: UserProfilePreferences = UserProfilePreferences()


class IssueOpen(BaseModel):
    """A tool-confirmed issue for `merge_post_call` to open or update.

    Carries no `opened_call`/`opened_at`: those are stamped by the merge
    from the session it is merging, which is the only place they are known
    to be true.

    Deliberately declares no length caps of its own. Constructing the
    `OpenIssue` that actually gets stored applies
    `app.domain.types.OpenIssue`'s caps, so the bounds are declared in one
    place. A caller exceeding them raises `ValidationError` out of the
    merge — correct, because this parameter is filled by pipeline code
    composing tool results, and an over-long summary there is a bug in our
    code rather than something a merchant said.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    summary: str
    status: str


class UserProfileMemory:
    """Memory layer 5 over `user_profiles` (docs/09 §5). Read the module
    docstring before changing the merge — every clause in it is a §5.2
    sentence or a named judgment call.

    **`user_id` is trusted, not verified.** Both methods act on whatever
    id they are handed; the row is keyed by it, so passing another
    merchant's id reads and writes another merchant's profile. That is the
    same shape as every repo in app/data/repositories, and there is no
    live caller yet — but the wiring task must source this from
    `SessionUser.user_id` (the JWT `sub`, app/domain/types.py), never from
    anything request-supplied, or it is an IDOR the day it lands. Stated
    here rather than assumed, because `SemanticMemoryProto` took the
    stronger route of making the principal a required typed parameter and
    this class deliberately did not — a profile read is keyed by the
    principal rather than merely filtered by it, so there is no second
    unscoped query shape for a `SessionUser` parameter to prevent.
    """

    def __init__(self, repo: UserProfileRepo) -> None:
        self._repo = repo

    # ------------------------------------------------------------------
    # Read path — call-setup prefetch (docs/09 §5.1, docs/02 §3.1)
    # ------------------------------------------------------------------

    async def load(self, user_id: str) -> UserProfile:
        """This merchant's profile, or a well-defined empty one.

        An absent row returns `UserProfile(user_id=...)` — empty facts,
        empty preferences, no open issues, `updated_at`/`updated_by_call`
        both `None`. That is a merchant's first call and it is not an
        error.

        A present row is laundered through the closed schema (see the
        module docstring): unknown keys dropped, every string flattened, a
        section that will not validate degraded to empty with a warning.
        Whatever this returns is therefore single-line, control-character
        free, and shaped like the domain type, regardless of what is
        actually in the row.

        Raising is still possible for a *caller* error rather than a data
        one: `user_id=""` fails `UserProfile`'s `min_length=1`. An empty
        principal is a bug at the call site, not a missing profile, and is
        not smoothed over here.
        """
        row = await self._repo.get(user_id)
        if row is None:
            return UserProfile(user_id=user_id)

        return UserProfile(
            user_id=row.user_id,
            facts=_validate_section(UserProfileFacts, row.facts, user_id=user_id, field="facts"),
            preferences=_validate_section(
                UserProfilePreferences, row.preferences, user_id=user_id, field="preferences"
            ),
            open_issues=_validate_issues(row.open_issues, user_id=user_id),
            updated_at=row.updated_at,
            updated_by_call=row.updated_by_call,
        )

    # ------------------------------------------------------------------
    # Write path — post-call merge (docs/09 §5.2, §8)
    # ------------------------------------------------------------------

    async def merge_post_call(
        self,
        user_id: str,
        *,
        session_id: str,
        extraction: Mapping[str, Any] | None = None,
        opened_issues: Sequence[IssueOpen] = (),
        resolved_issue_ids: Sequence[str] = (),
    ) -> UserProfile:
        """Apply docs/09 §5.2's merge and write the whole row, returning
        the profile as stored.

        `extraction` is the extractor's raw JSON (`{"facts": {...},
        "preferences": {...}}`); it is validated here, which is what makes
        the closed schema real. `None` and `{}` both mean "the merchant
        stated nothing new this call" — a normal outcome, and not the same
        as "retract everything".

        `opened_issues` and `resolved_issue_ids` are tool-confirmed, from
        the pipeline rather than the extractor. Resolution is applied
        before opening, so an id appearing in both ends up open: a tool
        that closed an issue and then opened one under the same id in the
        same call has, in effect, re-opened it, and keeping it is the
        reading that loses no information.

        Idempotent, as docs/09 §8 requires: the merge folds onto the
        current row, so re-applying the same arguments recomputes the same
        row. `updated_at` is the one field a retry moves — it records when
        the write happened, which a retry genuinely changes.
        """
        current = await self.load(user_id)
        parsed = _validate_extraction(extraction, user_id=user_id)

        now = datetime.now(UTC)
        merged = UserProfile(
            user_id=user_id,
            facts=_merge_stated(current.facts, parsed.facts),
            preferences=_merge_stated(current.preferences, parsed.preferences),
            open_issues=_merge_issues(
                current.open_issues,
                opened_issues=opened_issues,
                resolved_issue_ids=resolved_issue_ids,
                session_id=session_id,
                now=now,
            ),
            updated_at=now,
            updated_by_call=session_id,
        )

        await self._repo.upsert(
            user_id,
            # `exclude_none` keeps unstated facts out of the row entirely
            # rather than storing explicit nulls, matching docs/09 §5.1's
            # canonical row (which lists only the keys Rajesh actually
            # gave). Absent and null read back identically through the
            # section models, so nothing downstream can tell them apart.
            facts=merged.facts.model_dump(mode="json", exclude_none=True),
            preferences=merged.preferences.model_dump(mode="json", exclude_none=True),
            open_issues=[issue.model_dump(mode="json") for issue in merged.open_issues],
            updated_at=now,
            updated_by_call=session_id,
        )
        return merged


# --------------------------------------------------------------------------
# Read-path validation helpers
# --------------------------------------------------------------------------


def _validate_section[SectionT: BaseModel](
    model: type[SectionT], raw: object, *, user_id: str, field: str
) -> SectionT:
    """Validate one JSONB section, degrading to empty on failure.

    The log carries the user id and the column name but **never the
    offending value**: it is merchant-influenced free text, structlog's
    `redact_pii` processor is documented as not yet wired
    (app/obs/logging.py), and a profile section is exactly the kind of
    content docs/14 §5.1 wants masked before it reaches a log sink.
    """
    try:
        return model.model_validate(_flatten_optional_section(raw))
    except ValidationError:
        log.warning("memory.user_profile.section_invalid", user_id=user_id, field=field)
        return model()


def _validate_extraction(raw: object, *, user_id: str) -> ProfileExtraction:
    """Validate the extractor's raw JSON through the closed schema,
    degrading a section that will not validate to empty.

    Degrading rather than raising, and per section rather than whole: the
    extraction is Haiku output derived from what a caller said, so an
    off-schema shape is a normal operational event, not a programming
    error. Letting it propagate would abort the entire `merge_post_call`
    — including the `opened_issues`/`resolved_issue_ids` work, which is
    tool-confirmed, composed by our own code, and has nothing to do with
    the extractor. A bad extraction should cost exactly "no new facts
    this call", which is also what docs/09 §8's "each stage is
    independently skippable" asks for at the pipeline level.

    Contrast `opened_issues`, which deliberately still raises: that
    parameter is filled by pipeline code, so a violation there is our bug
    and should be loud.
    """
    if not isinstance(raw, Mapping):
        if raw is not None:
            # A non-mapping is a shape the extractor should never emit;
            # `None` is the ordinary "nothing stated this call" case and
            # is not worth a warning.
            log.warning("memory.user_profile.extraction_not_a_mapping", user_id=user_id)
        return ProfileExtraction()

    return ProfileExtraction(
        facts=_validate_section(
            UserProfileFacts, raw.get("facts", {}), user_id=user_id, field="extraction.facts"
        ),
        preferences=_validate_section(
            UserProfilePreferences,
            raw.get("preferences", {}),
            user_id=user_id,
            field="extraction.preferences",
        ),
    )


def _validate_issues(raw: object, *, user_id: str) -> tuple[OpenIssue, ...]:
    """Validate `open_issues` entry by entry, dropping only what is
    malformed.

    Per-entry rather than whole-list because one bad entry should not cost
    a merchant every other live issue. A non-list column degrades to empty
    — there is no partial reading of, say, a JSON object here.

    Trims to `_MAX_OPEN_ISSUES` on the way out: a row written before this
    cap existed, or by any writer that is not `merge_post_call`, can hold
    more than `UserProfile` will accept, and this read must not raise on
    it.
    """
    if not isinstance(raw, list):
        log.warning("memory.user_profile.open_issues_not_a_list", user_id=user_id)
        return ()

    issues: list[OpenIssue] = []
    dropped = 0
    for entry in raw:
        try:
            issues.append(OpenIssue.model_validate(_flatten_issue(entry)))
        except ValidationError:
            dropped += 1
    if dropped:
        log.warning("memory.user_profile.open_issue_invalid", user_id=user_id, dropped=dropped)
    if len(issues) > _MAX_OPEN_ISSUES:
        log.warning(
            "memory.user_profile.open_issues_over_cap",
            user_id=user_id,
            stored=len(issues),
            cap=_MAX_OPEN_ISSUES,
        )
        issues = issues[-_MAX_OPEN_ISSUES:]
    return tuple(issues)


# --------------------------------------------------------------------------
# Merge helpers (docs/09 §5.2)
# --------------------------------------------------------------------------


def _merge_stated[SectionT: BaseModel](current: SectionT, stated: SectionT) -> SectionT:
    """Newest-stated wins, per field — but only where something was
    stated.

    A field the extraction left `None` is silence, not a retraction: the
    merchant simply did not mention their city on this call, and docs/09
    §5.2's conflict rule has no conflict to resolve. Only a non-`None`
    extracted value overwrites. The consequence, stated because it is a
    real limitation rather than an oversight: **there is no way to clear a
    fact through this path.** Correcting a wrong fact means stating the
    right one; erasing one is right-to-delete's job (docs/09 §10), which
    deletes the whole row.
    """
    updates = {
        name: value
        for name, value in stated
        if value is not None and getattr(current, name) != value
    }
    return current.model_copy(update=updates) if updates else current


def _merge_issues(
    current: tuple[OpenIssue, ...],
    *,
    opened_issues: Sequence[IssueOpen],
    resolved_issue_ids: Sequence[str],
    session_id: str,
    now: datetime,
) -> tuple[OpenIssue, ...]:
    """Close, then open/update, then evict to the cap.

    See the module docstring for why closing removes the entry, why an
    update keeps the original `opened_call`/`opened_at`, and why overflow
    is drop-oldest.
    """
    resolved = {_flatten(issue_id) for issue_id in resolved_issue_ids}
    merged = [issue for issue in current if issue.id not in resolved]

    touched: set[str] = set()
    for opened in opened_issues:
        # `id` is flattened like every other stored string. It has to be:
        # `load` flattens it on the way back out, so an unflattened id
        # written here would not equal the id read next call, the
        # existing-issue lookup below would miss, and a retried pipeline
        # would append a duplicate instead of updating — breaking docs/09
        # §8's no-op requirement.
        issue_id = _flatten(opened.id)
        summary = _flatten(opened.summary)
        status = _flatten(opened.status)
        touched.add(issue_id)

        existing = next((i for i, issue in enumerate(merged) if issue.id == issue_id), None)
        if existing is None:
            merged.append(
                OpenIssue(
                    id=issue_id,
                    summary=summary,
                    status=status,
                    opened_call=session_id,
                    opened_at=now,
                )
            )
            continue
        # Rebuilt through the constructor, NOT `model_copy(update=...)`:
        # Pydantic's `model_copy` does not re-run field validation, so
        # `OpenIssue`'s length caps would simply not apply on this branch
        # and an over-long summary would reach the row uncapped.
        #
        # Provenance is carried over from the existing entry rather than
        # re-stamped — `opened_at` is when the issue opened, not when it
        # was last touched, and re-stamping it would make a retried
        # pipeline produce a different row (docs/09 §8 again).
        merged[existing] = OpenIssue(
            id=merged[existing].id,
            summary=summary,
            status=status,
            opened_call=merged[existing].opened_call,
            opened_at=merged[existing].opened_at,
        )

    return tuple(_evict_to_cap(merged, protected=touched))


def _evict_to_cap(issues: list[OpenIssue], *, protected: set[str]) -> list[OpenIssue]:
    """Drop-oldest FIFO to `_MAX_OPEN_ISSUES`, skipping issues this merge
    just touched.

    Plain drop-oldest has a bad case that is easy to hit and hard to
    notice: a profile sitting at the cap where one call both updates its
    oldest issue (a tool confirmed something about it) and opens a new
    one would evict the very issue the call just confirmed. Protecting
    touched ids means eviction falls on entries nothing has said anything
    about, which is the closest available proxy for "least live".

    The fallback matters and is not decoration: if every entry is
    protected the list is still over `UserProfile`'s hard `max_length`,
    so the tail is taken regardless. Bounded beats complete — the domain
    type would otherwise raise and fail the whole post-call merge.
    """
    if len(issues) <= _MAX_OPEN_ISSUES:
        return issues

    surplus = len(issues) - _MAX_OPEN_ISSUES
    kept: list[OpenIssue] = []
    for issue in issues:
        if surplus and issue.id not in protected:
            surplus -= 1
            continue
        kept.append(issue)
    return kept[-_MAX_OPEN_ISSUES:]


__all__ = ["IssueOpen", "ProfileExtraction", "UserProfileMemory"]
