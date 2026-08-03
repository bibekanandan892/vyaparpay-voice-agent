package com.vyaparpay.feature.dashboard

import com.vyaparpay.core.ui.testtag.TestTagRole
import com.vyaparpay.core.ui.testtag.TestTagRoles
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class DashboardDestinationTest {

    @Test
    fun `the dashboard route maps to the home flow from docs 03 section 3_8`() {
        assertEquals("home", DashboardDestination.FLOW)
    }

    @Test
    fun `the dashboard is an operational screen and therefore captured`() {
        assertTrue(DashboardDestination.IS_CAPTURED)
    }

    @Test
    fun `the root tag is a screen container, not a mis-typed interactive role`() {
        // A screen root that accidentally matched an interactive convention
        // (say `dashboard_cta`) would show up in the IR as a primary CTA.
        assertEquals(
            TestTagRole.UNRESOLVED,
            TestTagRoles.roleFor(DashboardDestination.ROOT_TEST_TAG),
        )
    }
}
