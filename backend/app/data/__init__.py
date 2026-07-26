"""Data-layer public surface — the async engine/sessionmaker factory and
the typed Redis client (docs/04-backend-architecture.md §4-6). Downstream
code imports from `app.data` rather than reaching into `app.data.engine`
/ `app.data.redis_client` directly, so this module is the one place the
public surface is pinned (mirrors `app/models/__init__.py` and
`app/obs/__init__.py`'s existing pattern).
"""

from __future__ import annotations

from app.data.engine import create_engine_and_sessionmaker
from app.data.redis_client import RedisClient, enforce_rate

__all__ = [
    "RedisClient",
    "create_engine_and_sessionmaker",
    "enforce_rate",
]
