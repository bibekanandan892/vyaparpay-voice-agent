package com.vyaparpay.core.screencontext

import com.vyaparpay.core.analytics.AppEvent
import com.vyaparpay.core.analytics.AppEventType
import com.vyaparpay.core.network.ApiError
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class AppContextStateTest {

    @Test
    fun `a freshly constructed state is not publishable`() {
        assertFalse(AppContextState().isPublishable)
    }

    @Test
    fun `a state with a route is publishable`() {
        assertTrue(AppContextState(route = "PaymentScreen", flow = "vendor_payment").isPublishable)
    }

    @Test
    fun `the state carries both cross-module facts docs 03 section 1 defends`() {
        val state = AppContextState(
            route = "PaymentScreen",
            flow = "vendor_payment",
            recentEvents = listOf(
                AppEvent.Tap(name = "pay_now_cta", ts = 1_784_536_440_000L, screen = "PaymentScreen"),
            ),
            lastApiError = ApiError.DAILY_LIMIT_EXCEEDED,
        )

        assertEquals(AppEventType.TAP, state.recentEvents.single().type)
        assertEquals(ApiError.DAILY_LIMIT_EXCEEDED, state.lastApiError)
    }
}
