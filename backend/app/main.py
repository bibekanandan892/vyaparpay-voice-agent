"""ASGI entrypoint the Dockerfile's `CMD` targets: `uvicorn app.main:app`.

The real app factory + lifespan lives in `app/api/main.py`, per the
Phase-2 plan's Batch-3.3 file ownership
(`app/api/{deps,middleware,main}.py`). This module exists purely so the
already-frozen `backend/Dockerfile` (Batch 1.2, committed before this
batch — its own comment: "app.main:app doesn't exist yet... it lands in a
later batch... no later task needs to touch this file") resolves without
a Dockerfile edit, which is out of scope for this task's file list.

Not merged into `app/api/main.py` itself and imported from there instead,
because `app.main` (this exact dotted path, one level above `app.api`) is
what the frozen `CMD` string names — nothing else satisfies it.
"""

from __future__ import annotations

from app.api.main import app, create_app

__all__ = ["app", "create_app"]
