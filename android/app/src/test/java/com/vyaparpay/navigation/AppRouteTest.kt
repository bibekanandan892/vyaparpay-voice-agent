package com.vyaparpay.navigation

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class AppRouteTest {

    @Test
    fun `the six demo screens (docs 03 section 4 plus OrderTrackingScreen, Phase-4 T5b) are all reachable`() {
        assertEquals(6, AppRoute.entries.size)
    }

    @Test
    fun `route strings are unique, so the nav graph cannot shadow a destination`() {
        val routes = AppRoute.entries.map { it.route }
        assertEquals(routes.size, routes.toSet().size)
    }

    @Test
    fun `support is the only destination excluded from capture`() {
        assertEquals(listOf(AppRoute.SUPPORT), AppRoute.entries.filterNot { it.isCaptured })
    }

    // Rewrite (Phase-4 T5b, part B). The old version of this test required
    // every captured destination's flow to be distinct -- not actually true
    // to docs/03 §3.8's own design: `flow` is a nav-graph GROUP, not a
    // per-screen key, and that section's route table already lists
    // OrdersScreen and OrderTrackingScreen on one shared `device_orders` row.
    // `route` (checked above) is what disambiguates individual screens in the
    // wire IR, not `flow`. The real invariant this test should guard is
    // narrower: ONLY device_orders is allowed to group more than one captured
    // destination -- a different flow silently gaining a second screen is
    // still very likely a bug and should keep failing this test.
    @Test
    fun `only the device_orders flow groups more than one captured destination, per docs 03 section 3_8`() {
        val byFlow = AppRoute.captured.groupBy(AppRoute::flow)

        val flowsWithMultipleDestinations = byFlow.filterValues { it.size > 1 }

        assertEquals(setOf("device_orders"), flowsWithMultipleDestinations.keys)
        assertEquals(
            setOf(AppRoute.ORDERS, AppRoute.ORDER_TRACKING),
            flowsWithMultipleDestinations.getValue("device_orders").toSet(),
        )
    }

    @Test
    fun `order tracking shares the device_orders flow with orders, by design`() {
        assertEquals(AppRoute.ORDERS.flow, AppRoute.ORDER_TRACKING.flow)
    }

    @Test
    fun `the start destination is the dashboard`() {
        assertEquals(AppRoute.DASHBOARD, AppRoute.START)
    }

    @Test
    fun `an unknown route resolves to null rather than a wrong destination`() {
        assertNull(AppRoute.fromRoute("SomeScreenWeRemoved"))
        assertNull(AppRoute.fromRoute(null))
    }

    @Test
    fun `a known route round-trips`() {
        assertEquals(AppRoute.PAYMENT, AppRoute.fromRoute(AppRoute.PAYMENT.route))
    }
}
