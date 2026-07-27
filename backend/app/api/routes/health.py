"""`GET /health` — a trivial liveness probe.

Not in docs/13's contract (that document only covers the `/v1` business
API) but standard practice for the container/deploy layer to poll — no
auth (`AuthMiddleware` special-cases this exact path), no
`{success,data,error,meta}` envelope, just a flat `{"status": "ok"}`.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


__all__ = ["router"]
