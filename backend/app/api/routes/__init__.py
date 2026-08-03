"""Route aggregation — one `APIRouter` per resource, assembled here into
the single `api_router` `app/api/main.py`'s `create_app()` mounts.

Phase-2 scope discipline (the plan's decision #8): exactly 4 business
endpoints (`GET /v1/wallet`, `GET /v1/payments/{id}`, `POST /v1/payments`,
`POST /v1/limits/increase-requests`) plus the unauthenticated `GET
/health`. No list/settlements/orders/complaints/cards endpoints — those
are out of scope even though docs/13 describes them.

Phase 3 adds the four session-lifecycle endpoints (docs/13 §2) from
`sessions.py` — the connect-bundle mint, the reconnect re-mint, the
idempotent hang-up, and the post-call summary.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routes.health import router as health_router
from app.api.routes.limits import router as limits_router
from app.api.routes.payments import router as payments_router
from app.api.routes.sessions import router as sessions_router
from app.api.routes.wallet import router as wallet_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(wallet_router)
api_router.include_router(payments_router)
api_router.include_router(limits_router)
api_router.include_router(sessions_router)

__all__ = ["api_router"]
