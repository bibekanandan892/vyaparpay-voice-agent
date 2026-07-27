"""Repository-per-aggregate data-access layer
(docs/04-backend-architecture.md §5).

Re-exports every repository class — plus the plain frozen dataclasses a
couple of them return (`PaymentStatus`, `LimitContext`, `DailyLimitCheck`,
`LimitIncreaseRequest`) — so callers write
`from app.data.repositories import PaymentRepo` instead of reaching into
individual modules; mirrors the `app.models` package's own re-export
convention.
"""

from __future__ import annotations

from app.data.repositories.base import SqlAlchemyRepository
from app.data.repositories.conversation_repo import ConversationRepo
from app.data.repositories.cost_repo import CostRepo
from app.data.repositories.limit_repo import LimitIncreaseRequest, LimitRepo
from app.data.repositories.merchant_repo import MerchantRepo
from app.data.repositories.payment_repo import (
    DailyLimitCheck,
    LimitContext,
    PaymentRepo,
    PaymentStatus,
)
from app.data.repositories.tool_audit_repo import ToolAuditRepo
from app.data.repositories.wallet_repo import WalletDebitResult, WalletRepo

__all__ = [
    "ConversationRepo",
    "CostRepo",
    "DailyLimitCheck",
    "LimitContext",
    "LimitIncreaseRequest",
    "LimitRepo",
    "MerchantRepo",
    "PaymentRepo",
    "PaymentStatus",
    "SqlAlchemyRepository",
    "ToolAuditRepo",
    "WalletDebitResult",
    "WalletRepo",
]
