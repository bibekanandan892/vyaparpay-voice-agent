package com.vyaparpay.core.screencontext

import com.vyaparpay.core.analytics.AppEvent
import com.vyaparpay.core.analytics.AppEventType
import com.vyaparpay.core.network.ApiError
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
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

    // ------------------------------------------------------------------
    // screen (Phase-4 T7b — landed once ScreenContextIr existed, T6)
    // ------------------------------------------------------------------

    @Test
    fun `a freshly constructed state has no screen`() {
        assertNull(AppContextState().screen)
    }

    @Test
    fun `a state carries the retained operational screen IR`() {
        val screen = ScreenContextIr(
            screen = "PaymentScreen",
            flow = "vendor_payment",
            components = listOf(ScreenComponent.PrimaryCta(label = "Pay Now", enabled = true)),
            lastAction = null,
            lastApi = null,
            dirtyFields = emptyList(),
            loading = false,
        )

        val state = AppContextState(route = "PaymentScreen", flow = "vendor_payment", screen = screen)

        assertEquals(screen, state.screen)
    }

    @Test
    fun `screen and route can legitimately disagree -- the retained-screen contract`() {
        // AppStateManager retains the last OPERATIONAL screen while route
        // moves to an excluded surface (docs/07 §2.1) — the two fields are
        // allowed to point at different screens by design; see this class's
        // own kdoc.
        val retainedScreen = ScreenContextIr(
            screen = "PaymentScreen",
            flow = "vendor_payment",
            components = emptyList(),
            lastAction = null,
            lastApi = null,
            dirtyFields = emptyList(),
            loading = false,
        )

        val state = AppContextState(route = "HelpScreen", flow = "support", screen = retainedScreen)

        assertEquals("HelpScreen", state.route)
        assertEquals("PaymentScreen", state.screen?.screen)
    }
}
