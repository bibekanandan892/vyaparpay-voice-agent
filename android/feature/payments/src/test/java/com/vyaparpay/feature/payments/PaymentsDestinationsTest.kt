package com.vyaparpay.feature.payments

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class PaymentsDestinationsTest {

    private val allFlows = listOf(
        PaymentDestination.FLOW,
        SettlementsDestination.FLOW,
        OrdersDestination.FLOW,
    )

    private val allRoutes = listOf(
        PaymentDestination.ROUTE,
        SettlementsDestination.ROUTE,
        OrdersDestination.ROUTE,
    )

    @Test
    fun `each route carries the flow name docs 03 section 3_8 assigns it`() {
        assertEquals("vendor_payment", PaymentDestination.FLOW)
        assertEquals("settlements_review", SettlementsDestination.FLOW)
        assertEquals("device_orders", OrdersDestination.FLOW)
    }

    @Test
    fun `flows are distinct, so the agent can tell these three screens apart`() {
        assertEquals(allFlows.size, allFlows.toSet().size)
    }

    @Test
    fun `routes are distinct, so the nav graph cannot silently collide`() {
        assertEquals(allRoutes.size, allRoutes.toSet().size)
    }

    @Test
    fun `all three are operational screens and therefore captured`() {
        assertTrue(PaymentDestination.IS_CAPTURED)
        assertTrue(SettlementsDestination.IS_CAPTURED)
        assertTrue(OrdersDestination.IS_CAPTURED)
    }
}
