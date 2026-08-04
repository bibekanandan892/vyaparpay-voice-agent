package com.vyaparpay.feature.payments

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class PaymentsDestinationsTest {

    private val allDestinations = listOf(
        PaymentDestination.ROUTE to PaymentDestination.FLOW,
        SettlementsDestination.ROUTE to SettlementsDestination.FLOW,
        OrdersDestination.ROUTE to OrdersDestination.FLOW,
        OrderTrackingDestination.ROUTE to OrderTrackingDestination.FLOW,
    )

    private val allRoutes = allDestinations.map { it.first }

    @Test
    fun `each route carries the flow name docs 03 section 3_8 assigns it`() {
        assertEquals("vendor_payment", PaymentDestination.FLOW)
        assertEquals("settlements_review", SettlementsDestination.FLOW)
        assertEquals("device_orders", OrdersDestination.FLOW)
        assertEquals("device_orders", OrderTrackingDestination.FLOW)
    }

    @Test
    fun `routes are distinct, so the nav graph cannot silently collide`() {
        assertEquals(allRoutes.size, allRoutes.toSet().size)
    }

    // Rewrite (Phase-4 T5b, part B). The old version of this test asserted
    // every destination's flow was distinct, which is not actually what
    // docs/03 §3.8 specifies: `flow` is a nav-graph GROUP, not a per-screen
    // key, and that section's own route table lists OrdersScreen and
    // OrderTrackingScreen on one shared `device_orders` row by design --
    // `ROUTE` (checked above) is what disambiguates individual screens in the
    // wire IR, not `flow`. The real invariant worth guarding is narrower:
    // ONLY device_orders is allowed to group more than one screen; any other
    // flow silently gaining a second screen is still almost certainly a bug
    // and should keep failing this test.
    @Test
    fun `only the device_orders flow groups more than one screen, per docs 03 section 3_8`() {
        val byFlow = allDestinations.groupBy({ it.second }, { it.first })

        val flowsWithMultipleScreens = byFlow.filterValues { it.size > 1 }

        assertEquals(setOf("device_orders"), flowsWithMultipleScreens.keys)
        assertEquals(
            setOf(OrdersDestination.ROUTE, OrderTrackingDestination.ROUTE),
            flowsWithMultipleScreens.getValue("device_orders").toSet(),
        )
    }

    @Test
    fun `all four are operational screens and therefore captured`() {
        assertTrue(PaymentDestination.IS_CAPTURED)
        assertTrue(SettlementsDestination.IS_CAPTURED)
        assertTrue(OrdersDestination.IS_CAPTURED)
        assertTrue(OrderTrackingDestination.IS_CAPTURED)
    }
}
