"""The harness's verdict vocabulary and its summary renderer.

A `Check` is one pinned assumption, its verdict, and the evidence behind
that verdict — plus the provider judgment call it cross-references, which
is the whole point of the exercise (a bare PASS tells an operator nothing
about which decision it just de-risked).

Everything here is pure: legs build and return `list[Check]`, and the CLI
renders and scores that list. No shared mutable report object.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

_RULE_WIDTH = 78


class Verdict(StrEnum):
    """PASS/FAIL are claims about the vendor. UNKNOWN is a claim about the
    RUN: the live stream never exercised this, so nothing was learned
    (harness judgment call 5 — UNKNOWN never fails the run)."""

    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class Check:
    """One pinned assumption, checked against the live endpoint."""

    check_id: str
    """Stable id, e.g. "DG-3" — quotable in a bug report."""
    validates: str
    """The provider judgment call / PINNED item this de-risks."""
    claim: str
    """The assumption, stated so a FAIL reads as news."""
    verdict: Verdict
    detail: str
    """What was actually observed. The evidence, not a restatement."""


def check(
    check_id: str, validates: str, claim: str, verdict: Verdict, detail: str
) -> Check:
    """Positional-friendly constructor — legs build these in bulk."""
    return Check(
        check_id=check_id, validates=validates, claim=claim, verdict=verdict, detail=detail
    )


def render_summary(checks: list[Check]) -> str:
    """The operator-facing table: one block per check, verdict first so a
    FAIL is findable by eye in a long scroll."""
    lines = ["", "=" * _RULE_WIDTH, " LIVE PROVIDER SMOKE — SUMMARY", "=" * _RULE_WIDTH]
    for item in checks:
        lines.append(f"[{item.verdict:^7}] {item.check_id}  (validates: {item.validates})")
        lines.append(f"          claim:    {item.claim}")
        lines.append(f"          observed: {item.detail}")
    lines.append("-" * _RULE_WIDTH)
    lines.append(f" {_tally(checks)}")
    if any(c.verdict is Verdict.UNKNOWN for c in checks):
        lines.append(
            " UNKNOWN = the live run never exercised this, NOT a pass. Re-run with"
        )
        lines.append(" --wav <real speech> to convert the transcript-dependent ones.")
    lines.append("=" * _RULE_WIDTH)
    return "\n".join(lines)


def _tally(checks: list[Check]) -> str:
    counts = {v: sum(1 for c in checks if c.verdict is v) for v in Verdict}
    return " ".join(f"{verdict}: {n}" for verdict, n in counts.items())


def exit_code(checks: list[Check]) -> int:
    """1 iff some pinned assumption was actively contradicted. UNKNOWN and
    an empty run both exit 0 — neither is evidence of a wrong assumption
    (exit 2 is the CLI's own "you invoked me wrong" code)."""
    return 1 if any(c.verdict is Verdict.FAIL for c in checks) else 0


__all__ = ["Check", "Verdict", "check", "exit_code", "render_summary"]
