"""`settlements` category — 8 articles (docs/17-roadmap.md §2.5).

Grounded in what's actually built: the T+1 cycle and "processing" ->
6 PM IST completion example (docs/01-product-and-use-case.md §7's
"same capability on other screens" table), `SettlementsScreen`'s batch
row / status chip / shortfall flag roles (docs/01 §5), and the
`get_settlements` / `raise_complaint` / `generate_invoice` tools
(docs/10-tool-calling.md §3). The 0.3% platform fee has no grounding doc —
invented once here and reused verbatim (`kb_settlements_fees`) rather than
re-invented per article.
"""

from __future__ import annotations

from scripts.kb_content.types import ArticleSpec

ARTICLES: tuple[ArticleSpec, ...] = (
    ArticleSpec(
        slug="kb_settlements_overview",
        title="How settlements work on VyaparPay",
        category="settlements",
        body_md="""\
## What a settlement is

A settlement is the transfer of your QR and UPI collections from
VyaparPay into your linked bank account, batched and processed on a T+1
cycle.

## What's included in a batch

Each settlement batch bundles a day's worth of successful collections,
minus any platform fees, refunds, or chargebacks that apply to that batch.

## Where to check settlements

SettlementsScreen lists each batch with its status, amount, and any
shortfall flags. Asha can also read out your latest batch status during a
support call.

## Still need help?

Ask Asha to pull your latest batch status, or raise a complaint from
HelpScreen referencing the batch id if something looks off.
""",
    ),
    ArticleSpec(
        slug="kb_settlements_t1_cycle",
        title="Understanding the T+1 settlement cycle",
        category="settlements",
        body_md="""\
## What T+1 means

Settlements move on a T+1 cycle: collections made on a given day are
settled to your bank account on the next business day, typically
completing by 6 PM IST.

## Why not same-day

The one-day gap covers bank-side reconciliation and fraud checks before
funds are released — this is standard across UPI-based settlement rails,
not specific to VyaparPay.

## What can push a settlement past 6 PM

Bank holidays, weekends, and occasional bank-side processing delays can
push a batch past its usual 6 PM completion time. See "What to do if a
settlement is late or short" for next steps.

## Still need help?

Ask Asha whether today's batch is still inside the normal T+1 window
before assuming something's wrong.
""",
    ),
    ArticleSpec(
        slug="kb_settlements_late_or_short",
        title="What to do if a settlement is late or short",
        category="settlements",
        body_md="""\
## First check the batch status

Open SettlementsScreen and look at the status chip for the batch in
question — "processing" means it's still inside the normal T+1 window.

## If it's late

A batch still processing well past 6 PM IST on its expected day,
especially outside a bank holiday, is worth flagging. Raise a complaint
from HelpScreen, or ask Asha to raise one during a call.

## If it's short

A settled amount lower than your collections total for that day is
usually explained by refunds, chargebacks, or platform fees deducted from
the batch — see "Understanding settlement shortfall flags".

## What support can do

Asha can pull the batch status and, if something looks genuinely wrong
rather than explained by a normal deduction, raise a complaint with a
reference number for follow-up.
""",
    ),
    ArticleSpec(
        slug="kb_settlements_shortfall_flags",
        title="Understanding settlement shortfall flags",
        category="settlements",
        body_md="""\
## What a shortfall flag means

SettlementsScreen marks a batch with a shortfall flag when the settled
amount is lower than the day's raw collections total.

## Common causes

The most common causes are refunds issued against that day's collections,
chargebacks, and the platform fee deducted before settlement — all of
these reduce the net amount without indicating an error.

## When a shortfall is worth escalating

If the shortfall amount doesn't match any refund or chargeback you're
aware of, raise a complaint referencing the batch id so it can be
investigated.

## Still need help?

Ask Asha for the fee and deduction breakdown behind a specific shortfall
before raising a complaint about it.
""",
    ),
    ArticleSpec(
        slug="kb_settlements_batch_status",
        title="Reading your settlement batch status",
        category="settlements",
        body_md="""\
## Reading the status chip

Each batch on SettlementsScreen shows one of a few statuses: processing
(within the normal T+1 window), settled (funds credited to your bank
account), or disputed (a complaint is open against it).

## Batch id

Every batch has an id shown on SettlementsScreen — have this ready when
raising a complaint or asking Asha to check a specific batch, since it's
the fastest way to look up the right one.

## Multiple batches in view

SettlementsScreen shows recent batches together, so check the date on
each one — a batch you're worried about might already be one or two days
old and simply superseded by a newer one in the list.

## Still need help?

Ask Asha to read out a specific batch's status by its batch id during a
support call.
""",
    ),
    ArticleSpec(
        slug="kb_settlements_fees",
        title="How settlement fees are calculated",
        category="settlements",
        body_md="""\
## The platform fee

VyaparPay deducts a 0.3% platform fee from QR and UPI collections before
they're settled to your bank account — this is the most common reason a
settled amount is slightly lower than your raw collections total.

## Where the fee shows up

The fee is reflected in the net settled amount on SettlementsScreen; it
is not a separate line-item transaction, so you won't see it as its own
entry in your transaction history.

## Fee questions during a support call

Asha can pull a batch's fee breakdown and explain exactly how the settled
amount was calculated from your raw collections for that day.

## Still need help?

Ask Asha to walk through the 0.3% fee calculation for a specific batch if
the deducted amount doesn't add up for you.
""",
    ),
    ArticleSpec(
        slug="kb_settlements_disputes",
        title="Disputing a settlement amount",
        category="settlements",
        body_md="""\
## When to dispute a settlement

Dispute a settled amount when it doesn't match your own records and
isn't explained by the platform fee, a refund, or a chargeback you're
aware of.

## How to raise a dispute

Raise a complaint from HelpScreen with the batch id and the amount you
expected, or ask Asha to do this for you during a call — she'll read back
a complaint reference number.

## What happens next

A disputed batch is reviewed against the underlying collection and fee
records. You'll be notified of the outcome, and you can check status
anytime via your complaint reference number.

## Still need help?

Keep your complaint reference number handy — it's the fastest way for
Asha or a human agent to pick up an open dispute.
""",
    ),
    ArticleSpec(
        slug="kb_settlements_gst_invoice",
        title="Generating a GST invoice for your settlements",
        category="settlements",
        body_md="""\
## What the GST invoice covers

A monthly GST invoice summarizes your settled collections, fees, and
taxes for a given calendar month, for your own accounting and filing
needs.

## How to generate one

Ask Asha to generate an invoice for a specific month, or request it from
HelpScreen. Invoice generation runs as a background job — it isn't
instant, since it compiles a full month of settlement data.

## Where the invoice goes

Once ready, the invoice is available in the app; if the job finishes
during a support call, Asha will let you know before you hang up.

## Still need help?

Ask Asha to check on an invoice job's status if it's been generating for
longer than expected.
""",
    ),
)

__all__ = ["ARTICLES"]
