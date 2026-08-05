"""Memory layer (docs/09-memory-architecture.md) — the working state a
call accumulates and persists. `ShortTermMemory` is Layer 1, the
in-process per-turn scratch space; `SessionMemory` is Layer 2, the
business-shape wrapper over `RedisClient`'s `session:{id}` hash fields.
`SemanticMemory` is Layer 6, the pgvector retriever over the seeded support
KB plus this merchant's own past-call summaries (docs/09 §6.2).

Layers 3-5 (rolling summary, conversation-summary store, user profile,
docs/09 §4-§5) are still Phase-5 scope and do not live here yet.
"""

from __future__ import annotations

from app.memory.semantic import SemanticMemory, prefetch_query, turn_query
from app.memory.session_memory import SessionMemory
from app.memory.short_term import ShortTermMemory

__all__ = [
    "SemanticMemory",
    "SessionMemory",
    "ShortTermMemory",
    "prefetch_query",
    "turn_query",
]
