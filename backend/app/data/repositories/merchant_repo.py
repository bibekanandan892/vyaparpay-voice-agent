"""MerchantRepo — repository for `merchants` (docs/12-data-models.md §3.1).

docs/04-backend-architecture.md §5's repository table doesn't name a
`MerchantRepo` — Phase 2's mapping there is `ConversationRepo`/
`WalletRepo`/`PaymentRepo`/`LimitRepo`/`ToolAuditRepo`/`CostRepo` only.
This repo is the Phase-2 plan's own addition (Batch 2 task 2.2), needed
because `ContextBuilder`'s `user_profile` prompt slot (docs/11
§1/§2 — business_name, city, account_type, preferred_language) reads
straight off this one row and nothing upstream of it does that join.
"""

from __future__ import annotations

from app.data.repositories.base import SqlAlchemyRepository
from app.models import Merchant


class MerchantRepo(SqlAlchemyRepository[Merchant]):
    """`get(merchant_id)` (inherited) is the only method `ContextBuilder`
    needs in Phase 2; `add`/`update` (also inherited) exist for the seed
    script and are otherwise unused — merchants aren't self-service-
    editable yet."""

    model = Merchant
