"""`SafetyLayer` — the deterministic guardrail wrapping every turn on
three sides: input (`fence_input`), output (`screen_output`), and action
(`classify_affirmation` + `authorize_tool`). Implements
`app.domain.interfaces.SafetyLayerProto` verbatim.

docs/05-agent-architecture.md §3.6 owns the check/stage/action table this
class implements; docs/10-tool-calling.md §1 (invariants 1 and 3) and §4
(the confirm gate's affirmation step) own the semantics. Everything here
is deterministic regex/set machinery — **no LLM calls** (Phase-2 plan
decision #5): a guardrail that itself calls a model would inherit the
model's failure modes, and every check below is either structural or a
pattern the eval fixtures can replay byte-for-byte. The Haiku-assisted
affirmation upgrade path (docs/05 §3.4's "intent classification" utility
row) plugs in *behind* `classify_affirmation`'s frozen signature later,
without changing any caller.

Phase-2 honesty notes (judgment calls, flagged per house style):

1. **`fence_input` scans slots that are empty in Phase 2.**
   `screen_context`/`recent_actions` are Phase 3/4 slots
   (`app/domain/types.py:ContextBundle`); the heuristics below are
   exercised only by tests until those pipelines land. The scan is built
   now anyway so the seam exists and is tested before real untrusted
   screen text ever flows through it.
2. **`current_utterance` is deliberately NOT neutralized.** It is
   untrusted (canon §12) but it is also the caller's actual request —
   blanking it kills the turn, and PromptBuilder renders it inside a data
   fence as the structural defense (docs/05 §3.6 row 1). The *action*
   protections (principal injection, the confirm gate, the tool
   allowlist) are what make acting on a hostile utterance safe; a
   keyword filter over speech would add false positives, not safety.
3. **`screen_output`'s amount check is digits-only.** Word-form amounts
   ("twenty-five thousand") are out of scope for the deterministic
   Phase-2 checker — parsing spoken-number English/Hinglish reliably
   needs the LLM-assisted path. Every ₹/Rs/rupee *digit* amount and
   every fully-formed reference id (`LMT-…`, `txn_…`) must trace to this
   turn's tool results; partial readbacks ("reference ending 0913") are
   not extracted and therefore not audited — known Phase-2 thinness.
"""

from __future__ import annotations

import re
from typing import Any, Final

from app.domain.interfaces import ToolRegistry
from app.domain.types import (
    ContextBundle,
    PendingConfirm,
    SafetyVerdict,
    SessionUser,
    ToolCall,
    ToolResult,
)
from app.obs.logging import get_logger

log = get_logger(__name__)

# --------------------------------------------------------------------------
# Input fence (docs/05 §3.6 rows 1-2)
# --------------------------------------------------------------------------

# The ContextBundle slots whose *content* originates outside our trust
# boundary (screen labels, app event names — docs/14's threat model).
# `current_utterance` is deliberately absent: see module docstring note 2.
_UNTRUSTED_SLOTS: Final = ("screen_context", "recent_actions")

# Imperative injection heuristics (docs/05 §3.6 row 2: "imperative
# patterns, 'ignore previous', tool-name strings"). Deliberately a short,
# high-precision list: the fence's *structural* defense is PromptBuilder's
# data fencing; this scan only has to catch text that is unambiguously
# trying to be an instruction, where a false negative is survivable and a
# false positive erases a (Phase 3/4) screen slot for one turn.
_INJECTION_PATTERNS: Final = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bignore\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\b",
        r"\bdisregard\s+(?:all\s+|your\s+)?(?:previous|prior|instructions|rules)\b",
        r"\bforget\s+(?:everything|all|your)\b",
        r"\bsystem\s+prompt\b",
        r"\byou\s+are\s+now\b",
        r"\bnew\s+instructions?\b",
        r"\bact\s+as\b",
    )
)

_NEUTRALIZED_TEMPLATE: Final = "[neutralized: injection heuristics matched in {slot}]"


def looks_injected(text: str) -> bool:
    """Whether `text` matches an imperative injection heuristic above.

    Public and module-level so the memory-slot renderers
    (`app/memory/slots.py`) can screen durable stored text against the
    *same* pattern list rather than growing a second copy that drifts.

    Deliberately NOT the whole of `SafetyLayer._looks_injected`: that
    method also flags any registered tool name appearing verbatim, on the
    reasoning that "screen labels have no legitimate reason to name
    internal tools". Stored memory does. `user_profiles.open_issues` is
    composed by post-call pipeline code from tool outcomes (docs/09 §5.2),
    so an issue summary naming `request_limit_increase` is the field
    working as designed, and a KB article about raising a limit may name
    the same tool. Applying the tool-name half there would false-positive
    on correct content, which for a durable store is the expensive
    direction — see `app/memory/slots.py` for what the remedy is and why
    it is per-entry.
    """
    return any(pattern.search(text) for pattern in _INJECTION_PATTERNS)

# --------------------------------------------------------------------------
# Output checks (docs/05 §3.6 rows 3-4; docs/10 §1 invariant 1)
# --------------------------------------------------------------------------

# Voiced-amount extraction: ₹-prefixed, Rs./INR-prefixed, or "N rupees"
# suffixed digit amounts. Digits only — module docstring note 3.
_AMOUNT_PATTERN: Final = re.compile(
    r"(?:₹|\bRs\.?\s*|\bINR\s+)\s*(\d[\d,]*(?:\.\d+)?)", re.IGNORECASE
)
_RUPEE_SUFFIX_PATTERN: Final = re.compile(
    r"\b(\d[\d,]*(?:\.\d+)?)\s*rupees\b", re.IGNORECASE
)

# Reference-id extraction: the two shapes Phase-2 tools emit — LMT-style
# dashed uppercase ids (docs/10 §3.1's `^LMT-\d{4}-\d{4}-\d{4}$`,
# generalized one notch so a future `CMP-…` id is caught too) and
# `txn_…` payment ids. Kept deliberately narrow: a catch-all
# `prefix_suffix` pattern would flag ordinary snake_case speech.
_REFERENCE_PATTERNS: Final = (
    re.compile(r"\b[A-Z]{2,5}-\d{4}-\d{4}-\d{4}\b"),
    re.compile(r"\btxn_[a-z0-9_]+\b"),
)

# Numeric tokens inside tool-result *strings* (e.g. a gate summary's
# "raise daily limit from ₹25,000 to ₹50,000") count as verified numbers.
_NUMERIC_TOKEN_PATTERN: Final = re.compile(r"\d[\d,]*(?:\.\d+)?")

# PII masks (docs/05 §3.6 row 4; canon §12). Order is load-bearing: the
# 16-digit card pattern must run before the 12-digit Aadhaar pattern so a
# card number is masked whole rather than its first 12 digits matching as
# Aadhaar. `\b` on both ends keeps a 12-digit pattern from matching
# inside a longer digit run. PAN is India's tax id (5 letters, 4 digits,
# 1 letter) — not the card-industry PAN, which this system never stores
# (docs/10 §3.1: the column does not exist).
_PII_PATTERNS: Final = (
    re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b"),  # 16-digit card
    re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}\b"),  # 12-digit Aadhaar
    # Code-review LOW, fixed: every other pattern in this module is
    # case-insensitive; this one wasn't, so a lowercase-rendered PAN
    # (e.g. from a tool that lowercases text) passed through unmasked.
    re.compile(r"\b[A-Za-z]{5}\d{4}[A-Za-z]\b"),  # PAN (Indian tax id)
)
_PII_MASK: Final = "****"

# --------------------------------------------------------------------------
# Affirmation classification (docs/10 §4 step 2; plan decision #5)
# --------------------------------------------------------------------------
#
# Deterministic keyword/regex classification, word-boundary anchored so
# "yesterday" never reads as a yes. The decision ladder (applied in
# order, first match wins):
#
#   1. no pending action        -> False (nothing to affirm)
#   2. negation present         -> False ("no", "don't", "cancel", "nahi")
#   3. hedge present            -> False ("hmm, maybe", "not sure")
#   4. modified terms present   -> False (digits/₹/lakh/"but"/"make it" —
#      "yes but make it a lakh" is a *new* proposal, not a clean yes;
#      docs/10 §4's supersession rule)
#   5. explicit affirmative     -> True ("yes", "go ahead", "haan", …)
#   6. anything else            -> False (ambiguity is not consent)
#
# Hindi/Hinglish tokens ("haan", "theek hai", "karo") are included
# because the canonical caller code-switches (docs/01); the list is a
# starting set, not a linguistics claim — the Haiku-assisted upgrade path
# (module docstring) is where real robustness lands.

_NEGATION_PATTERN: Final = re.compile(
    r"\b(?:no|nope|nah|don'?t|do\s+not|not|never|cancel|stop|wait|nahi|nahin|mat)\b",
    re.IGNORECASE,
)
_HEDGE_PATTERN: Final = re.compile(
    r"\b(?:maybe|perhaps|hmm+|umm+|hold\s+on|let\s+me\s+think|think\s+about|later)\b",
    re.IGNORECASE,
)
_MODIFIED_TERMS_PATTERN: Final = re.compile(
    r"\d|₹|\b(?:lakh|crore|thousand|hundred|but|instead|actually|rather"
    r"|make\s+it|change|increase|decrease)\b",
    re.IGNORECASE,
)
_AFFIRMATION_PATTERN: Final = re.compile(
    r"\b(?:yes|yeah|yep|yup|sure|ok(?:ay)?|confirm(?:ed)?|correct|absolutely|definitely"
    r"|go\s+ahead|do\s+it|please\s+do|proceed|haan|haanji|ji\s+haan|theek\s+hai|karo|kar\s+do)\b",
    re.IGNORECASE,
)

# --------------------------------------------------------------------------
# Action authorization (docs/10 §1 invariant 3, §2 authorize stage)
# --------------------------------------------------------------------------

# Principal-shaped argument keys the LLM must never supply — the executor
# injects the session user; a model-supplied identity key is a smuggling
# attempt (or an injection symptom) either way (docs/10 §1's own "why").
_PRINCIPAL_ARGUMENT_KEYS: Final = frozenset({"user_id", "merchant_id", "wallet_id", "principal"})


class SafetyLayer:
    """Implements `SafetyLayerProto` (app/domain/interfaces.py) with
    purely deterministic checks — see the module docstring for scope and
    the honesty notes on Phase-2 thinness."""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    # -- input ------------------------------------------------------------

    def fence_input(self, bundle: ContextBundle) -> ContextBundle:
        """Scan the untrusted slots for injection heuristics; a flagged
        slot is *neutralized* (replaced with an inert marker) in a new
        bundle — never an exception, never a dropped turn (docs/05 §3.6:
        "flag the slot suspicious, neutralize, record on the trace").
        Returns the bundle unchanged (same instance) when nothing matches.
        """
        updates: dict[str, str] = {}
        for slot in _UNTRUSTED_SLOTS:
            text: str = getattr(bundle, slot)
            if text and self._looks_injected(text):
                updates[slot] = _NEUTRALIZED_TEMPLATE.format(slot=slot)
                log.warning("input_fence_slot_neutralized", slot=slot)
        # Frozen model: model_copy builds the derived bundle, no mutation.
        return bundle.model_copy(update=updates) if updates else bundle

    def _looks_injected(self, text: str) -> bool:
        """Imperative patterns OR a registered tool name appearing
        verbatim in slot text — screen labels have no legitimate reason
        to name internal tools (docs/05 §3.6 row 2)."""
        if looks_injected(text):
            return True
        lowered = text.lower()
        return any(registered.name.lower() in lowered for registered in self._registry.all())

    # -- output -----------------------------------------------------------

    def screen_output(self, text: str, tool_results: list[ToolResult]) -> SafetyVerdict:
        """The no-unverified-account-facts check (docs/10 §1 invariant 1)
        plus the PII mask (docs/05 §3.6 row 4), fail-closed.

        Every ₹-amount and reference id extracted from the draft must
        appear in this turn's `tool_results` — searched across `data`,
        `error` (business-error `detail` is voiced, docs/10 §5), and
        `gate` (the confirmation summary carries the amounts the model
        must restate, docs/10 §6 T5.5). Numbers are compared canonically
        (₹25,000 == the int 25000 in `data`). An unverified fact blocks
        the utterance outright (`allowed=False`); PII is masked into
        `safe_text` (`allowed=True`, modified). If the check itself
        errors, the verdict is `allowed=False` — hedge and re-fetch
        beats voicing an unverified amount (docs/05 §3.6 failure
        behavior).
        """
        try:
            verified_numbers, verified_corpus = self._collect_verified_facts(tool_results)
            unverified = _find_unverified_facts(text, verified_numbers, verified_corpus)
            if unverified:
                log.warning("output_blocked_unverified_facts", facts=unverified)
                return SafetyVerdict(
                    allowed=False,
                    reason=f"unverified account facts: {', '.join(unverified)}",
                )
            masked = _mask_pii(text)
            if masked != text:
                log.warning("output_pii_masked")
                return SafetyVerdict(allowed=True, reason="pii_masked", safe_text=masked)
            return SafetyVerdict(allowed=True)
        except Exception:
            # Fail closed (docs/05 §3.6): a broken checker must never
            # wave an unverified amount through.
            log.exception("screen_output_check_failed")
            return SafetyVerdict(
                allowed=False, reason="output safety check failed internally; failing closed"
            )

    def _collect_verified_facts(
        self, tool_results: list[ToolResult]
    ) -> tuple[set[str], str]:
        """Walk every result payload (`data`/`error`/`gate`) collecting
        canonical numbers and a searchable string corpus. An instance
        method (not a free function) so tests can monkeypatch it to prove
        the fail-closed path.

        Security review CRITICAL, fixed: `result.error.detail` routinely
        echoes the LLM's OWN tool-call arguments back verbatim — e.g.
        `get_payment_status`'s 404 echoes the caller-supplied
        `payment_id`, and `request_limit_increase`'s `STALE_LIMIT_VIEW`
        echoes `current_limit * 100` as `current_view_paise`. Treating
        those digits as "verified" let the model launder an arbitrary
        chosen number through an error response, then voice it as an
        unrelated fact elsewhere in the same turn (PoC: call
        `get_payment_status(payment_id="txn_500000")` on a nonexistent
        id, then say "I found a settlement credit of ₹500,000" — the
        digit run inside the echoed `payment_id` string verified it).
        `error` payloads therefore contribute to the STRING corpus only
        (so a genuinely server-generated reference id, e.g.
        `LIMIT_REQUEST_ALREADY_PENDING`'s `existing_request_id`, still
        verifies via substring match — that id is never something the
        model supplied), never to the `numbers` set. `data` (the tool's
        own authoritative output — no current handler echoes a raw
        caller argument there) and `gate` (docs/10 §6 T5.5: the model
        must be able to voice its OWN just-proposed action's amounts
        back for confirmation — a narrower, same-turn, executor-gated
        surface, not an open echo channel) keep contributing to both.
        """
        numbers: set[str] = set()
        strings: list[str] = []
        for result in tool_results:
            if result.data:
                _walk_payload(result.data, numbers, strings)
            if result.error:
                _walk_payload(result.error, None, strings)
            if result.gate:
                _walk_payload(result.gate, numbers, strings)
        return numbers, "\n".join(strings)

    # -- action -----------------------------------------------------------

    def classify_affirmation(self, utterance: str, pending: PendingConfirm | None) -> bool:
        """True only for a clean, explicit yes to an existing pending
        action — see the decision ladder documented above the pattern
        set. Anything ambiguous, negated, hedged, or carrying modified
        terms is False; docs/10 §4: "an explicit affirmation …, not
        sentiment inference"."""
        if pending is None:
            return False
        normalized = utterance.strip()
        if not normalized:
            return False
        if _NEGATION_PATTERN.search(normalized):
            return False
        if _HEDGE_PATTERN.search(normalized):
            return False
        if _MODIFIED_TERMS_PATTERN.search(normalized):
            return False
        return _AFFIRMATION_PATTERN.search(normalized) is not None

    def authorize_tool(self, call: ToolCall, principal: SessionUser) -> bool:
        """Allowlist membership + the invariant-3 backstop: any
        principal-shaped key in the call's arguments is a rejection
        (docs/10 §1 — the model can only ever act as the caller).

        Scope notes: top-level argument keys only — nested smuggling is
        already stopped by every input model's `extra="forbid"` at the
        validation stage, and this check is the boundary backstop, not a
        deep scanner. Tier *policy* (read vs confirm vs control dispatch)
        deliberately does not live here — that is the executor's dispatch
        job (docs/10 §2's authorize-stage note); this method answers only
        "may this call, as shaped, proceed at all?". `principal` is
        unused in Phase 2 (single-merchant scoping is the executor's
        injection) but stays in the frozen signature for the multi-scope
        production evolution.
        """
        if self._registry.get(call.name) is None:
            log.warning("authorize_rejected_unknown_tool", tool=call.name)
            return False
        smuggled = _PRINCIPAL_ARGUMENT_KEYS & set(call.arguments)
        if smuggled:
            log.warning(
                "authorize_rejected_principal_keys", tool=call.name, keys=sorted(smuggled)
            )
            return False
        return True


# --------------------------------------------------------------------------
# Module-level helpers (pure functions — no self, no state)
# --------------------------------------------------------------------------


def _walk_payload(value: Any, numbers: set[str] | None, strings: list[str]) -> None:
    """Recursively harvest verified facts from one result payload. Ints
    and floats become canonical numbers; strings join the corpus *and*
    have their numeric tokens harvested (so "₹25,000" inside a gate
    summary verifies a later voiced ₹25,000). Dict keys are structure,
    not facts — skipped. `bool` is excluded before `int` (it subclasses
    int) so `true` never verifies a voiced "1".

    `numbers=None` (security review CRITICAL fix, see
    `_collect_verified_facts`): still builds the string corpus — a
    payload can carry a genuinely server-generated reference id worth
    matching by substring — but skips ALL numeric harvesting, both raw
    int/float values and digit-tokens extracted from strings. Used for
    `result.error`, which routinely echoes the model's own tool-call
    arguments back and must never license them as verified account
    facts."""
    if isinstance(value, bool):
        return
    if isinstance(value, int | float):
        if numbers is not None:
            numbers.add(_canonical_number(value))
    elif isinstance(value, str):
        strings.append(value)
        if numbers is not None:
            for token in _NUMERIC_TOKEN_PATTERN.findall(value):
                numbers.add(_canonical_number_token(token))
    elif isinstance(value, dict):
        for item in value.values():
            _walk_payload(item, numbers, strings)
    elif isinstance(value, list | tuple):
        for item in value:
            _walk_payload(item, numbers, strings)


def _canonical_number(value: int | float) -> str:
    """One canonical string per numeric value so 25000, 25000.0, and
    "25,000" all compare equal. Phase-2 amounts are integer rupees ≤ ₹1
    crore (docs/10 §3), well inside float exactness."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _canonical_number_token(token: str) -> str:
    cleaned = token.replace(",", "")
    try:
        return _canonical_number(float(cleaned))
    except ValueError:  # not actually numeric — keep raw so it can't match
        return cleaned


def _find_unverified_facts(text: str, numbers: set[str], corpus: str) -> list[str]:
    """Every extracted amount/reference in `text` that does NOT trace to
    the verified set. Empty list == the draft is clean."""
    unverified: list[str] = []
    for pattern in (_AMOUNT_PATTERN, _RUPEE_SUFFIX_PATTERN):
        for match in pattern.finditer(text):
            if _canonical_number_token(match.group(1)) not in numbers:
                unverified.append(match.group(0))
    for reference_pattern in _REFERENCE_PATTERNS:
        for match in reference_pattern.finditer(text):
            if match.group(0) not in corpus:
                unverified.append(match.group(0))
    return unverified


def _mask_pii(text: str) -> str:
    """Card/Aadhaar/PAN masking (docs/05 §3.6 row 4) — pattern order is
    load-bearing, see `_PII_PATTERNS`. Builds a new string; never
    mutates."""
    masked = text
    for pattern in _PII_PATTERNS:
        masked = pattern.sub(_PII_MASK, masked)
    return masked


__all__ = ["SafetyLayer", "looks_injected"]
