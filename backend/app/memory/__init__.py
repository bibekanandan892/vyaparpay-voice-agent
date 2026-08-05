"""Memory layer (docs/09-memory-architecture.md) — the working state a
call accumulates and persists. `ShortTermMemory` is Layer 1, the
in-process per-turn scratch space; `SessionMemory` is Layer 2, the
business-shape wrapper over `RedisClient`'s `session:{id}` hash fields;
`UserProfileMemory` is Layer 5, the durable per-merchant profile in
Postgres (docs/09 §5); `SemanticMemory` is Layer 6, the pgvector
retriever over the seeded support KB plus this merchant's own past-call
summaries (docs/09 §6.2).

Still absent, so the gap is a statement rather than an assumption:
Layer 3 (the rolling `Summarizer` fold, docs/09 §4) and Layer 4's
durable `ConversationSummaryStore`. Both are Phase-5 scope
(docs/17-roadmap.md §1.3) and land in a sibling batch.
"""

from __future__ import annotations

from app.memory.semantic import SemanticMemory, prefetch_query, turn_query
from app.memory.session_memory import SessionMemory
from app.memory.short_term import ShortTermMemory
from app.memory.user_profile import IssueOpen, ProfileExtraction, UserProfileMemory

__all__ = [
    "IssueOpen",
    "ProfileExtraction",
    "SemanticMemory",
    "SessionMemory",
    "ShortTermMemory",
    "UserProfileMemory",
    "prefetch_query",
    "turn_query",
]
