"""`refunds` category — 8 articles (docs/17-roadmap.md §2.5).

No corresponding built feature exists yet (docs/01-product-and-use-case.md
§5's "unevenly" mapping — `refunds` and `kyc` are the two categories
without a live screen), beyond the `get_refund_status` read tool
(docs/10-tool-calling.md §3). Every numeric policy detail below (the
7-business-day SLA, the 30-day eligibility window) has no grounding doc —
each is invented exactly once and reused verbatim across every article
that references it, per the task's consistency requirement, rather than a
different plausible number per article.
"""

from __future__ import annotations

from scripts.kb_content.types import ArticleSpec

ARTICLES: tuple[ArticleSpec, ...] = (
    ArticleSpec(
        slug="kb_refunds_overview",
        title="How refunds work on VyaparPay",
        category="refunds",
        body_md="""\
## What a refund is

A refund reverses a QR or UPI collection you received from a customer,
sending the money back to them through VyaparPay.

## Who can request one

Only you, the merchant, can initiate a refund against a collection in
your own transaction history — customers request refunds from you
directly, and you action them in the app.

## Where to check refund status

Use the transaction in question on PaymentScreen or ask Asha for its
refund status by transaction id.

## Still need help?

Ask Asha to check a refund's status by transaction id, or raise a
complaint from HelpScreen if something looks stuck.
""",
    ),
    ArticleSpec(
        slug="kb_refunds_processing_time",
        title="How long a refund takes to process",
        category="refunds",
        body_md="""\
## The refund SLA

A refund typically completes within 7 business days of being initiated —
this is the standard window across UPI-based refund rails, not a
VyaparPay-specific delay.

## What affects the timing

Refunds to a VyaparPay wallet are usually much faster, often within a few
hours. Refunds back to a customer's bank account or card depend on their
bank's own processing time, up to the 7-business-day window.

## When to check back

If a refund hasn't completed within 7 business days, it's worth checking
status rather than assuming it's still processing — see "What to do if a
refund status looks stuck".

## Still need help?

Ask Asha for a refund's exact status if it's approaching or past the
7-business-day window.
""",
    ),
    ArticleSpec(
        slug="kb_refunds_status_stuck",
        title="What to do if a refund status looks stuck",
        category="refunds",
        body_md="""\
## First, check the current status

Ask Asha for the refund status on the transaction, or check it on
PaymentScreen — status will show as pending, succeeded, or failed.

## If it's still pending past 7 business days

A refund pending well beyond the standard 7-business-day window is worth
escalating. Raise a complaint referencing the original transaction id.

## If it shows failed

A failed refund did not reach the customer — see "Why a refund attempt
can fail" for common causes, then retry or raise a complaint if the cause
isn't clear.

## Still need help?

Have the original transaction id ready — it's the fastest way for Asha or
a human agent to trace a stuck refund.
""",
    ),
    ArticleSpec(
        slug="kb_refunds_customer_not_received",
        title="Customer says they haven't received their refund",
        category="refunds",
        body_md="""\
## Confirm the refund actually succeeded

Before assuming something's wrong, check the refund's status on your side
— if it shows "succeeded," the money has left VyaparPay and any further
delay is on the receiving bank's side.

## What to tell the customer

A succeeded refund to a bank account or card can still take a few extra
days to visibly post there, even after VyaparPay's own 7-business-day
window — this is normal for many banks.

## When to raise a complaint

If a refund shows "succeeded" for well over 7 business days and the
customer still hasn't seen it, raise a complaint with the transaction id
so it can be traced.

## Still need help?

Ask Asha to confirm a refund's succeeded status before escalating a
customer's report that it never arrived.
""",
    ),
    ArticleSpec(
        slug="kb_refunds_partial",
        title="Partial refunds — how they work",
        category="refunds",
        body_md="""\
## What a partial refund is

A partial refund returns less than the full original payment amount —
useful when only part of an order is being returned or adjusted.

## How it's requested

Partial refunds go through the same flow as a full refund; you specify
the amount to return, which must be less than or equal to the original
transaction amount.

## Multiple partial refunds on one transaction

A single transaction can have more than one partial refund against it, as
long as the total refunded never exceeds the original amount.

## Still need help?

Ask Asha how much of a transaction has already been refunded before
issuing another partial refund against it.
""",
    ),
    ArticleSpec(
        slug="kb_refunds_failed",
        title="Why a refund attempt can fail",
        category="refunds",
        body_md="""\
## Common reasons a refund fails

A refund attempt can fail if the original transaction is too old for the
refund window, if it's already been fully refunded, or if the customer's
receiving account or card is no longer valid.

## What VyaparPay does on a failure

A failed refund does not move any money — your wallet balance is
unaffected, and you can retry once the underlying issue (for example, an
expired card) is resolved.

## Getting help

Ask Asha to check the specific failure reason for a transaction, or raise
a complaint if retrying doesn't resolve it.

## Still need help?

Ask Asha for the exact failure reason before retrying — it saves a second
failed attempt for the same cause.
""",
    ),
    ArticleSpec(
        slug="kb_refunds_window",
        title="The refund eligibility window",
        category="refunds",
        body_md="""\
## The refund eligibility window

A collection can be refunded within 30 days of the original transaction
date. After that window, VyaparPay's refund tool can no longer action it
directly.

## Why there's a window at all

The window matches the reconciliation period most banks and UPI rails
keep for reversing a transaction cleanly; refunding well past it becomes
a manual bank-side process outside VyaparPay's control.

## What to do past the window

If a customer needs money back on a transaction older than 30 days, raise
a complaint from HelpScreen so a manual resolution path can be explored.

## Still need help?

Ask Asha to confirm whether a specific transaction is still inside the
30-day refund window before attempting a refund.
""",
    ),
    ArticleSpec(
        slug="kb_refunds_wallet_vs_original_source",
        title="Refund to wallet vs original payment source",
        category="refunds",
        body_md="""\
## Two possible refund destinations

A refund either returns to the customer's original payment source (their
bank account or UPI app) or, for wallet-funded collections, credits back
to the VyaparPay wallet involved.

## Why the destination matters for timing

Wallet-to-wallet refunds are typically much faster — often within a few
hours — since no external bank rail is involved. Refunds to an external
bank account or UPI app follow the full 7-business-day window.

## You don't choose the destination

The refund destination is determined automatically by how the original
payment was funded — it's not a setting you pick when initiating the
refund.

## Still need help?

Ask Asha which destination applies to a specific refund if the timing
looks different from what you expected.
""",
    ),
)

__all__ = ["ARTICLES"]
