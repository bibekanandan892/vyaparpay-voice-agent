"""The shared `chars / 3.5` token-estimate proxy (docs/07-ui-semantic-context.md
§7, docs/08-context-and-events.md §4.3): "Token measurement uses the same
`chars / 3.5` proxy with 10% margin as the client — the constant is
documented in protocol/ so both sides over-estimate identically."

No exact tokenizer runs on this hot path (`context.build`'s 15/40 ms p50/p95
budget, docs/08 §4.3) — this is a deliberate over-estimate, safe in the
direction that matters: undershooting a token budget risks a busy screen
silently blowing the 2,500-token turn budget (docs/08 §7 failure-mode row),
while over-estimating only costs an occasional unnecessary drop-ladder rung.

Judgment call: this is the first concrete implementation of the proxy
anywhere in `backend/`. `app/agent/cost_tracker.py`'s own docstring (judgment
call #3) names the identical `chars/3.5` clause from docs/05 §3.8 but
deliberately defers implementing it (that module's frozen `record_turn`
signature never receives turn text). `protocol/` does not yet carry a
machine-readable constant for it either, despite docs/08 §4.3's claim — this
module is that constant's first concrete home on the backend. A future task
unifying the client/CostTracker/ContextCompressor proxies into one shared
location is reasonable, but out of this task's scope (this task owns
`app/context/` only).
"""

from __future__ import annotations

import math
from typing import Final

# docs/07 §7 / docs/08 §4.3: "a `chars / 3.5` proxy with a 10% safety
# margin... over-estimates slightly on JSON punctuation, which is the safe
# direction."
CHARS_PER_TOKEN: Final = 3.5
SAFETY_MARGIN: Final = 1.10


def estimate_tokens(text: str) -> int:
    """Over-estimate of `text`'s token count via the canon chars/3.5 proxy,
    rounded up so the estimate never *understates* a budget check."""
    return math.ceil((len(text) / CHARS_PER_TOKEN) * SAFETY_MARGIN)


__all__ = ["CHARS_PER_TOKEN", "SAFETY_MARGIN", "estimate_tokens"]
