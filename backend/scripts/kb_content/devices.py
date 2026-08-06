"""`devices` category — 8 articles (docs/17-roadmap.md §2.5).

Grounded in `OrdersScreen`'s built role (docs/01-product-and-use-case.md
§5: order id, tracking state, ETA) and the `get_orders` / `track_device_order`
tools (docs/10-tool-calling.md §3). Dispatch/return SLAs (5 business days
dispatch, 10-day return window) have no grounding doc — invented once here
and reused verbatim across every article that references them.
"""

from __future__ import annotations

from scripts.kb_content.types import ArticleSpec

ARTICLES: tuple[ArticleSpec, ...] = (
    ArticleSpec(
        slug="kb_devices_overview",
        title="Soundbox and QR kit orders — an overview",
        category="devices",
        body_md="""\
## What device orders cover

VyaparPay sells soundboxes and QR kits for accepting in-person payments —
OrdersScreen lists every device order you've placed, with its current
status.

## Ordering a device

Order a soundbox or QR kit directly from OrdersScreen; you'll get an
order id you can use to track it.

## After delivery

Once a device arrives, see "Setting up your soundbox after delivery" to
get it collecting payments.

## Still need help?

Ask Asha to check an order's status by order id, or raise a complaint
from HelpScreen if something's gone wrong.
""",
    ),
    ArticleSpec(
        slug="kb_devices_tracking",
        title="Tracking your device order",
        category="devices",
        body_md="""\
## Where to track an order

OrdersScreen shows each order's tracker state — placed, dispatched, in
transit, or delivered — along with an estimated delivery date.

## Getting courier details

Ask Asha to track a specific order by order id; she can pull the courier
name, tracking id, and current status for you.

## What "in transit" means

"In transit" means the device has left the warehouse and is with the
courier — it does not mean it's out for delivery that specific day; check
the estimated delivery date for that.

## Still need help?

Ask Asha to pull the latest courier scan for an order if its status
hasn't moved in a while.
""",
    ),
    ArticleSpec(
        slug="kb_devices_delivery_delay",
        title="What to do if your device delivery is delayed",
        category="devices",
        body_md="""\
## Normal dispatch timing

Soundbox and QR kit orders are typically dispatched within 5 business
days of order confirmation, then delivered by the courier partner.

## When a delay is worth flagging

If an order hasn't dispatched within 5 business days, or hasn't moved
past "in transit" well beyond its estimated delivery date, raise a
complaint referencing the order id.

## What Asha can do

Asha can pull the latest courier scan for an order during a call and tell
you exactly where it's stuck, if anywhere.

## Still need help?

Raise a complaint from HelpScreen with the order id if dispatch or
delivery is running well past the normal timing.
""",
    ),
    ArticleSpec(
        slug="kb_devices_damaged_or_faulty",
        title="Device arrived damaged or faulty",
        category="devices",
        body_md="""\
## If a device arrives damaged

Report a damaged or faulty device as soon as you notice it — raise a
complaint from HelpScreen with the order id and a short description, or
ask Asha to do it for you.

## What happens next

A damaged or faulty device is typically replaced rather than repaired;
see "Requesting a replacement device" for the process.

## Don't discard the original packaging

Keep the original packaging until a replacement is confirmed — it may be
needed for the faulty unit's return.

## Still need help?

Ask Asha to raise a complaint on your behalf, referencing the order id
and the specific issue.
""",
    ),
    ArticleSpec(
        slug="kb_devices_replacement",
        title="Requesting a replacement device",
        category="devices",
        body_md="""\
## When a replacement is offered

A replacement device is offered when the original arrives damaged, is
faulty on first use, or is confirmed lost in transit.

## How to request one

Raise a complaint referencing the original order id and the reason, and a
replacement order is created once the issue is confirmed.

## Timing

A replacement follows the same dispatch timeline as a new order —
typically dispatched within 5 business days of being confirmed.

## Still need help?

Ask Asha to check a replacement order's status the same way you'd check
any other device order.
""",
    ),
    ArticleSpec(
        slug="kb_devices_setup",
        title="Setting up your soundbox after delivery",
        category="devices",
        body_md="""\
## Unboxing and powering on

Charge the soundbox fully before first use, then power it on — it will
prompt you to pair it with your VyaparPay account.

## Linking to your account

Follow the in-app pairing flow from OrdersScreen once the device is
powered on; this links the soundbox to your merchant account so it can
announce payments correctly.

## Testing it

Send yourself a small test QR collection after setup to confirm the
soundbox announces the payment correctly before relying on it with real
customers.

## Still need help?

Ask Asha to walk through the pairing steps if the soundbox isn't
announcing payments after setup.
""",
    ),
    ArticleSpec(
        slug="kb_devices_multiple_orders",
        title="Ordering devices for multiple counters",
        category="devices",
        body_md="""\
## Ordering for more than one counter

You can place multiple device orders on the same account — useful for a
shop with more than one billing counter, or additional QR kits for
different sections.

## Tracking multiple orders at once

Each order gets its own order id and tracker state on OrdersScreen, so
you can follow them independently even if they were placed together.

## Delivery timing for bulk orders

Multiple devices ordered together are usually dispatched together, but
can arrive on slightly different days depending on courier routing.

## Still need help?

Ask Asha to check each order id separately if only one of several
counter orders looks delayed.
""",
    ),
    ArticleSpec(
        slug="kb_devices_returns",
        title="Returning an unused device order",
        category="devices",
        body_md="""\
## Returning an unused device

An unopened, unused device order can be returned within 10 days of
delivery for a refund to your original payment method.

## How to start a return

Raise a complaint referencing the order id and mention it's a return
request; support will confirm the return process and pickup, where
available.

## What doesn't qualify for return

A device that's been used, or one reported as damaged or faulty, follows
the replacement process instead of a standard return — see "Requesting a
replacement device".

## Still need help?

Ask Asha to confirm whether an order is still inside the 10-day return
window before starting a return.
""",
    ),
)

__all__ = ["ARTICLES"]
