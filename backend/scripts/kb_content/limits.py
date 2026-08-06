"""`limits` category — 8 articles (docs/17-roadmap.md §2.5).

The category the canonical incident (docs/01-product-and-use-case.md §7-8:
Rajesh, ₹245 -> Amazon Business, `402 DAILY_LIMIT_EXCEEDED`) lives in, so
this is the one category where every fact is pinned to canon rather than
invented: the ₹25,000 Merchant Pro daily limit, the ₹50,000 upgrade tier,
the "usually reviewed within 4 business hours" SLA (docs/01 §8 turn 5,
docs/10-tool-calling.md §3.1's `request_limit_increase` contract), and the
midnight-IST reset (`app/data/repositories/payment_repo.py`'s
`_midnight_ist_after`). `kb_limits_daily_limit_exceeded` is the article the
retrieval test in `tests/scripts/test_seed_kb.py` proves is findable for a
query resembling the canonical incident.

Numbers with no grounding doc (Merchant Basic's ₹10,000/₹25,000 tier, the
per-transaction sub-limit) are invented once here and reused verbatim by
every other article in this package that references them — never a second,
differently-invented figure — per the task's consistency requirement.
"""

from __future__ import annotations

from scripts.kb_content.types import ArticleSpec

ARTICLES: tuple[ArticleSpec, ...] = (
    ArticleSpec(
        slug="kb_limits_daily_txn_overview",
        title="Understanding your daily transaction limit",
        category="limits",
        body_md="""\
## What the daily transaction limit is

Every VyaparPay wallet has a daily transaction limit set by your linked
bank account, separate from your wallet balance. It caps the total value
of payments you can send out in a single calendar day, resetting at
midnight IST.

## How it's different from your wallet balance

Your wallet balance is money already sitting in your VyaparPay account.
Your daily limit is a bank-imposed cap on how much of that money can move
out in one day. A payment can fail on the limit even when your wallet
balance is more than enough to cover it.

## Default limits by account type

Merchant Basic accounts start with a ₹10,000 daily transaction limit.
Merchant Pro accounts start with a ₹25,000 daily transaction limit. Both
can be increased — see "How to request a daily limit increase".

## Where to check your current limit and usage

Open PaymentScreen or DashboardScreen to see today's limit and how much of
it you've already used. You can also ask Asha during a support call.

## Still need help?

Ask Asha during a support call, or raise a complaint from HelpScreen if
something about your limit still looks wrong after checking here.
""",
    ),
    ArticleSpec(
        slug="kb_limits_daily_limit_exceeded",
        title="Why a payment fails with Daily Limit Exceeded",
        category="limits",
        body_md="""\
## What DAILY_LIMIT_EXCEEDED means

A payment declined with the DAILY_LIMIT_EXCEEDED error (HTTP 402) means
today's total spending has already reached your bank-imposed daily
transaction limit. The payment did not go through, and no money left your
wallet.

## Why this can happen even with money in your wallet

Your wallet balance and your daily limit are two separate things. If
you've already sent ₹24,890 of a ₹25,000 daily limit, even a ₹245 payment
will be declined until midnight IST, no matter how much sits in your
wallet.

## What to do right now

You have two options: wait until the limit resets at midnight IST and
retry the payment, or request a daily limit increase so today's payment
can go through sooner. See "How to request a daily limit increase" for the
second option.

## How support resolves this

During a support call, Asha checks your wallet balance and the exact
decline detail — the limit, how much you've used today, and when it
resets — using live account data, then offers to submit a limit increase
request on your behalf.

## Still need help?

Ask Asha during a support call, or raise a complaint from HelpScreen and
reference the declined payment if the explanation here doesn't match what
you're seeing.
""",
    ),
    ArticleSpec(
        slug="kb_limits_wallet_vs_bank_limit",
        title="Wallet balance vs. daily bank limit — what's the difference",
        category="limits",
        body_md="""\
## Two different numbers on the same screen

PaymentScreen shows your wallet balance and, if a payment fails, a
bank-limit error — these are two independent numbers, and confusing them
is the single most common reason a decline feels wrong.

## Wallet balance

This is money already credited to your VyaparPay wallet from past
collections and settlements. It only goes down when a payment actually
succeeds.

## Daily bank transaction limit

This is a cap your linked bank applies to how much can move out of your
account in one calendar day, regardless of wallet balance. It resets at
midnight IST.

## The contradiction, explained

A payment can be declined on the bank limit while your wallet balance is
comfortably higher than the payment amount. The wallet balance is not the
number that decided the decline.

## Still need help?

Ask Asha during a support call — she can read out both numbers from your
account and confirm which one is affecting a specific payment.
""",
    ),
    ArticleSpec(
        slug="kb_limits_requesting_increase",
        title="How to request a daily limit increase",
        category="limits",
        body_md="""\
## When to request an increase

Request a limit increase when you need to make a payment today that would
otherwise be blocked by DAILY_LIMIT_EXCEEDED, or when your daily volume
regularly bumps against your current limit.

## How the tiers work

Merchant Basic accounts can request an increase from ₹10,000 to ₹25,000
per day. Merchant Pro accounts can request an increase from ₹25,000 to
₹50,000 per day.

## Review time

A limit increase request is usually reviewed within 4 business hours.
You'll get a notification, and Asha can check the status for you on a
later call.

## What you need to confirm

Because this changes your account limit, both the app and Asha ask you to
explicitly confirm the request — the current limit, the requested limit —
before it's submitted. You'll receive a reference number once it's in.

## Still need help?

Ask Asha to submit the request for you during a support call, or raise a
complaint from HelpScreen if a submitted request doesn't show up.
""",
    ),
    ArticleSpec(
        slug="kb_limits_merchant_pro_vs_basic",
        title="Merchant Basic vs Merchant Pro transaction limits",
        category="limits",
        body_md="""\
## The two account types

VyaparPay merchants are either Merchant Basic or Merchant Pro. The account
type controls your default daily transaction limit and the limit-increase
tier you're eligible for.

## Merchant Basic

Starts at a ₹10,000 daily transaction limit, upgradeable to ₹25,000 on
request.

## Merchant Pro

Starts at a ₹25,000 daily transaction limit, upgradeable to ₹50,000 on
request. Merchant Pro is intended for higher-volume shops with regular
vendor payouts.

## Upgrading your account type

Ask support about upgrading from Merchant Basic to Merchant Pro if your
daily volume consistently needs a higher ceiling than repeated
limit-increase requests can comfortably cover.

## Still need help?

Ask Asha during a support call which account type you're currently on and
whether an upgrade makes sense for your volume.
""",
    ),
    ArticleSpec(
        slug="kb_limits_per_txn_limit",
        title="Per-transaction limit vs daily limit",
        category="limits",
        body_md="""\
## Two kinds of limit

Alongside the daily cumulative limit, VyaparPay also enforces a
per-transaction limit — the maximum a single payment can move, independent
of how much of your daily limit remains.

## Per-transaction limit by account type

Merchant Basic: ₹5,000 per transaction. Merchant Pro: ₹15,000 per
transaction.

## Why a large single payment can fail even with daily headroom

An ₹18,000 vendor payment on a Merchant Basic account fails on the
per-transaction cap even if the full ₹10,000 daily limit hasn't been
touched yet that day — the two limits are checked independently.

## Increasing your per-transaction limit

Per-transaction limit increases go through the same request-and-review
process as the daily limit, and are usually reviewed within 4 business
hours.

## Still need help?

Ask Asha which limit — daily or per-transaction — actually blocked a
specific payment before requesting an increase.
""",
    ),
    ArticleSpec(
        slug="kb_limits_reset_schedule",
        title="When does my daily limit reset",
        category="limits",
        body_md="""\
## When limits reset

Your daily transaction limit usage resets to zero at midnight IST every
day, regardless of your account type or current limit tier.

## What carries over and what doesn't

Only your daily limit usage resets. Your wallet balance, your limit tier
(₹25,000 vs ₹50,000, for example), and any pending limit-increase request
all carry over unchanged.

## Planning around the reset

If a payment is declined late in the day on DAILY_LIMIT_EXCEEDED and it
can wait, retrying after midnight IST is often faster than requesting an
increase, which takes up to 4 business hours to review.

## Still need help?

Ask Asha how much of today's limit you have left before deciding whether
to wait for the reset or request an increase.
""",
    ),
    ArticleSpec(
        slug="kb_limits_pending_request_status",
        title="Checking the status of a pending limit increase request",
        category="limits",
        body_md="""\
## Checking a request you already submitted

Once a limit increase request is submitted, it moves to "submitted"
status while VyaparPay's review process runs, usually completing within 4
business hours.

## What "already pending" means

If you try to submit a second limit increase request while one is still
under review, VyaparPay returns a LIMIT_REQUEST_ALREADY_PENDING response
with your existing request's reference number rather than creating a
duplicate.

## Asking Asha for a status check

Asha can look up your pending request by reference number during a call
and tell you whether it's still under review or has been approved.

## If a request seems to be taking longer than expected

Requests are typically resolved well inside the 4-business-hour window.
If it's been longer, raise a complaint from HelpScreen or ask Asha to do
it for you.

## Still need help?

Have your request reference number ready — it's the fastest way for Asha
or a human agent to look up exactly where things stand.
""",
    ),
)

__all__ = ["ARTICLES"]
