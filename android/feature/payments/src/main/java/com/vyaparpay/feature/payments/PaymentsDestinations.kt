package com.vyaparpay.feature.payments

/**
 * The vendor-payment flow — where the canonical 402 lives (docs/03 §4).
 */
public object PaymentDestination {
    public const val ROUTE: String = "PaymentScreen"
    public const val FLOW: String = "vendor_payment"
    public const val ROOT_TEST_TAG: String = "payment_root"
    public const val IS_CAPTURED: Boolean = true
}

/** Settlement batches with status badges and net/fee breakdown. */
public object SettlementsDestination {
    public const val ROUTE: String = "SettlementsScreen"
    public const val FLOW: String = "settlements_review"
    public const val ROOT_TEST_TAG: String = "settlements_root"
    public const val IS_CAPTURED: Boolean = true
}

/** Device orders — soundbox, QR kit — with courier tracking state. */
public object OrdersDestination {
    public const val ROUTE: String = "OrdersScreen"
    public const val FLOW: String = "device_orders"
    public const val ROOT_TEST_TAG: String = "orders_root"
    public const val IS_CAPTURED: Boolean = true
}

/**
 * A single device order's tracking detail.
 *
 * [FLOW] deliberately equals [OrdersDestination.FLOW] — docs/03 §3.8's route
 * table lists `OrdersScreen`/`OrderTrackingScreen` on one shared
 * `device_orders` row: `flow` is a nav-graph GROUP, not a per-screen key, and
 * `ROUTE` (not `flow`) is what disambiguates individual screens in the wire
 * IR. See `PaymentsDestinationsTest`'s own rewritten assertion for why two
 * destinations sharing a `flow` is correct here, not a bug.
 */
public object OrderTrackingDestination {
    public const val ROUTE: String = "OrderTrackingScreen"
    public const val FLOW: String = "device_orders"
    public const val ROOT_TEST_TAG: String = "order_tracking_root"
    public const val IS_CAPTURED: Boolean = true
}
