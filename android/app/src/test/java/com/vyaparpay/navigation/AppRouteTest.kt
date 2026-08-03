package com.vyaparpay.navigation

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class AppRouteTest {

    @Test
    fun `the five demo screens of docs 03 section 4 are all reachable`() {
        assertEquals(5, AppRoute.entries.size)
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

    @Test
    fun `every captured destination has a distinct flow name`() {
        // The agent distinguishes screens by `flow`; two captured screens sharing
        // one would make the context ambiguous in exactly the moment it matters.
        val flows = AppRoute.captured.map { it.flow }
        assertEquals(flows.size, flows.toSet().size)
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
