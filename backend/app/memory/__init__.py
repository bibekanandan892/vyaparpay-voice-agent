"""Memory layer (docs/09-memory-architecture.md) — the working state a
call accumulates and persists. Mapped to docs/09 §1's seven-layer table:

- Layer 1, `ShortTermMemory` — the in-process per-turn scratch space.
- Layer 2, `SessionMemory` — the business-shape wrapper over
  `RedisClient`'s `session:{id}` hash fields.
- Layer 3, `Summarizer` (rolling, Redis, docs/09 §4) **plus**
  `ConversationSummaryStore` (final, Postgres, docs/09 §8). One layer,
  two stores — the table names both under "Conversation summary".
- Layer 5, `UserProfileMemory` — the durable per-merchant profile
  (docs/09 §5).
- Layer 6, `SemanticMemory` — the pgvector retriever over the seeded
  support KB plus this merchant's own past-call summaries (docs/09 §6.2).

Layers 4 and 7 have no class here and are not omissions: Layer 4 is
"long-term memory", the durable Postgres umbrella over `user_profiles` +
`conversation_summaries` + `memory_chunks`, which reaches the prompt only
via layers 5 and 6; Layer 7 is context compression, a cross-cutting
policy rather than a store, implemented across `Summarizer`,
`ContextCompressor`, `SemanticSnapshotBuilder` and `ToolExecutor`.
"""

from __future__ import annotations

from app.memory.conversation_summary_store import ConversationSummaryStore
from app.memory.semantic import SemanticMemory, prefetch_query, turn_query
from app.memory.session_memory import SessionMemory
from app.memory.short_term import ShortTermMemory
from app.memory.summarizer import Summarizer
from app.memory.user_profile import IssueOpen, ProfileExtraction, UserProfileMemory

__all__ = [
    "ConversationSummaryStore",
    "IssueOpen",
    "ProfileExtraction",
    "SemanticMemory",
    "SessionMemory",
    "ShortTermMemory",
    "Summarizer",
    "UserProfileMemory",
    "prefetch_query",
    "turn_query",
]
