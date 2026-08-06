"""`kyc` category — 8 articles (docs/17-roadmap.md §2.5).

Like `refunds`, no corresponding built feature or screen exists yet
(docs/01-product-and-use-case.md §5) beyond `merchants.kyc_status`'s three
values — `pending` / `verified` / `on_hold` (`ck_merchants_kyc_status`,
app/models/orm.py) — and the confirm-gated `update_business_address` tool
(docs/10-tool-calling.md §3). Every other detail (document list, the
3-business-day turnaround, the ~12-month re-verification cadence) is
invented once here and reused verbatim across every article that
references it.
"""

from __future__ import annotations

from scripts.kb_content.types import ArticleSpec

ARTICLES: tuple[ArticleSpec, ...] = (
    ArticleSpec(
        slug="kb_kyc_overview",
        title="KYC on VyaparPay — what it is and why it matters",
        category="kyc",
        body_md="""\
## What KYC is

KYC (Know Your Customer) verifies your identity and business details
before VyaparPay activates full account functionality — a standard
requirement across Indian fintech and banking products.

## Why it matters

KYC status affects what you can do on your account — an unverified or
on-hold account may have limits on transactions or settlements until
verification completes.

## Where to check your status

Your KYC status is shown in your account details under HelpScreen, and
Asha can confirm it during a support call.

## Still need help?

Ask Asha to confirm your current KYC status if you're not sure where you
stand.
""",
    ),
    ArticleSpec(
        slug="kb_kyc_statuses",
        title="Understanding your KYC status: pending, verified, on hold",
        category="kyc",
        body_md="""\
## The three statuses

A VyaparPay account's KYC status is one of: pending (verification in
progress), verified (fully approved), or on_hold (verification paused,
needs attention).

## Pending

Pending means your documents are submitted and under review — no action
is needed from you unless asked for additional information.

## Verified

Verified means your identity and business details are fully confirmed,
and your account has full functionality.

## On hold

On_hold means something needs your attention before verification can
finish — see "Why your account might be on KYC hold" for common reasons.

## Still need help?

Ask Asha which of the three statuses your account is currently in, and
what — if anything — is needed from you.
""",
    ),
    ArticleSpec(
        slug="kb_kyc_documents",
        title="Documents needed to complete KYC",
        category="kyc",
        body_md="""\
## Identity documents

VyaparPay requires a government-issued identity document — typically
Aadhaar or PAN — to confirm who the account belongs to.

## Business documents

For registered businesses, a GST certificate or equivalent business
registration proof is required alongside the owner's identity documents.

## Address proof

A recent business address proof is required, matching the address you
register on your account — see "Updating your registered business
address" if this changes later.

## Bank account proof

A cancelled cheque or bank statement confirming your linked bank account
is required so settlements can be verified against the right account.

## Still need help?

Ask Asha which document, if any, is still outstanding on your account.
""",
    ),
    ArticleSpec(
        slug="kb_kyc_on_hold",
        title="Why your account might be on KYC hold",
        category="kyc",
        body_md="""\
## Common reasons for an on-hold status

An account most often goes on_hold due to a document that doesn't match
the account's registered details, an expired identity document, or an
incomplete address proof.

## What to do

Check HelpScreen for the specific reason listed against your account,
then resubmit the corrected document. Asha can also confirm what's
missing during a support call.

## Impact while on hold

Some account functionality may be limited while on_hold — settlements
and higher-value transactions are the most commonly affected until
verification completes.

## Still need help?

Ask Asha to read out the exact hold reason on your account before
resubmitting anything.
""",
    ),
    ArticleSpec(
        slug="kb_kyc_turnaround",
        title="How long KYC verification takes",
        category="kyc",
        body_md="""\
## How long verification takes

KYC verification typically completes within 3 business days of
submitting all required documents.

## What can extend it

Verification can take longer if a submitted document is unclear,
mismatched, or if additional information is requested — the clock
effectively restarts once you resubmit.

## Checking progress

Ask Asha for your current KYC status during a call, or check HelpScreen —
there's no separate tracking number, since status is tied directly to
your account.

## Still need help?

Ask Asha for a status check if it's been longer than 3 business days
since you submitted your documents.
""",
    ),
    ArticleSpec(
        slug="kb_kyc_update_business_address",
        title="Updating your registered business address",
        category="kyc",
        body_md="""\
## When to update your address

Update your registered business address whenever you move locations or
open a new outlet under the same account, so your KYC records stay
accurate.

## How to update it

Use the update_business_address action from HelpScreen, or ask Asha to
submit the change during a call — she'll confirm the new address and
pincode before submitting.

## What happens after an update

An address change may trigger a fresh review of your address proof — see
"Why VyaparPay may ask you to re-verify KYC".

## Still need help?

Ask Asha to confirm an address update went through if you don't see it
reflected on your account afterward.
""",
    ),
    ArticleSpec(
        slug="kb_kyc_reverification",
        title="Why VyaparPay may ask you to re-verify KYC",
        category="kyc",
        body_md="""\
## Why re-verification happens

VyaparPay may ask you to re-verify KYC periodically — roughly once every
12 months — or whenever a significant account detail changes, such as
your registered business address.

## What re-verification involves

Re-verification usually just needs a fresh copy of your existing
documents, unless something has genuinely changed, in which case updated
documents are needed.

## It's not a sign of a problem

Being asked to re-verify is routine account hygiene, not an indication
that something is wrong with your account.

## Still need help?

Ask Asha to confirm why re-verification was triggered if it's unclear
from what's shown on HelpScreen.
""",
    ),
    ArticleSpec(
        slug="kb_kyc_failed_verification",
        title="What to do if KYC verification fails",
        category="kyc",
        body_md="""\
## If verification fails

A failed verification means the submitted documents couldn't be
confirmed — check HelpScreen for the specific reason, which is almost
always a document mismatch or image quality issue.

## Resubmitting

Correct the specific issue named and resubmit — most failed verifications
succeed on the next attempt once the actual mismatch is fixed.

## If it keeps failing

If verification fails more than once for reasons that aren't clear,
raise a complaint or ask Asha to escalate to a human agent for manual
review.

## Still need help?

Ask Asha to escalate to a human agent if a second resubmission still
fails for an unclear reason.
""",
    ),
)

__all__ = ["ARTICLES"]
