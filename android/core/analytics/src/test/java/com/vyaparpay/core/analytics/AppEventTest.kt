package com.vyaparpay.core.analytics

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Pins each `app_event/v1` variant's shape against
 * `protocol/schemas/app_event.v1.json` / docs/08 §2.1 — every field the wire
 * schema requires, and nothing it does not.
 */
class AppEventTest {

    @Test
    fun `nav carries type name ts and from`() {
        val event: AppEvent = AppEvent.Nav(name = "PaymentScreen", ts = 1_784_536_395_000L, from = "DashboardScreen")

        assertEquals(AppEventType.NAV, event.type)
        assertEquals("PaymentScreen", event.name)
        assertEquals(1_784_536_395_000L, event.ts)
        assertEquals("DashboardScreen", (event as AppEvent.Nav).from)
    }

    @Test
    fun `tap carries type name ts and screen`() {
        val event: AppEvent = AppEvent.Tap(name = "Pay Now", ts = 1_784_536_440_000L, screen = "PaymentScreen")

        assertEquals(AppEventType.TAP, event.type)
        assertEquals("Pay Now", event.name)
        assertEquals("PaymentScreen", (event as AppEvent.Tap).screen)
    }

    @Test
    fun `input value is null for a sensitive field the schema says to omit`() {
        val event = AppEvent.Input(name = "pin", ts = 1L)

        assertEquals(AppEventType.INPUT, event.type)
        assertNull(event.value)
    }

    @Test
    fun `input carries the committed value for a non-sensitive field`() {
        val event = AppEvent.Input(name = "amount", ts = 1_784_536_417_000L, value = "₹245")

        assertEquals("₹245", event.value)
    }

    @Test
    fun `api error carries method-plus-path as name, status, and code`() {
        val event = AppEvent.ApiErrorEvent(
            name = "POST /payments",
            ts = 1_784_536_440_820L,
            status = 402,
            code = "DAILY_LIMIT_EXCEEDED",
        )

        assertEquals(AppEventType.API_ERROR, event.type)
        assertEquals("POST /payments", event.name)
        assertEquals(402, event.status)
        assertEquals("DAILY_LIMIT_EXCEEDED", event.code)
    }

    @Test
    fun `dialog carries visible true on attach`() {
        val event = AppEvent.Dialog(name = "Daily Limit Exceeded", ts = 1_784_536_440_900L, visible = true)

        assertEquals(AppEventType.DIALOG, event.type)
        assertTrue(event.visible)
    }

    @Test
    fun `dialog carries visible false on detach`() {
        val event = AppEvent.Dialog(name = "Daily Limit Exceeded", ts = 2L, visible = false)

        assertEquals(false, event.visible)
    }

    @Test
    fun `a when over AppEvent is exhaustive across all five variants without an else branch`() {
        // This test's real assertion is that it compiles at all: if AppEventType
        // ever grows a sixth variant, the `when` below fails to compile until a
        // branch is added — the taxonomy is closed by contract (docs/08 §2.1).
        val events: List<AppEvent> = listOf(
            AppEvent.Nav(name = "a", ts = 0, from = "b"),
            AppEvent.Tap(name = "a", ts = 0, screen = "b"),
            AppEvent.Input(name = "a", ts = 0),
            AppEvent.ApiErrorEvent(name = "a", ts = 0, status = 500, code = "INTERNAL"),
            AppEvent.Dialog(name = "a", ts = 0, visible = false),
        )

        events.forEach { event ->
            val label: String = when (event) {
                is AppEvent.Nav -> "nav"
                is AppEvent.Tap -> "tap"
                is AppEvent.Input -> "input"
                is AppEvent.ApiErrorEvent -> "api_error"
                is AppEvent.Dialog -> "dialog"
            }
            assertEquals(event.type.name.lowercase(), label)
        }
    }
}
