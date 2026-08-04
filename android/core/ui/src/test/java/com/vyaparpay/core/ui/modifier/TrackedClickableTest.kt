package com.vyaparpay.core.ui.modifier

import com.vyaparpay.core.analytics.AppEvent
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * [trackedClickable]'s event-shape mapping, split into [tapEvent] so it is
 * testable without Compose UI test infrastructure (this module has none).
 */
class TrackedClickableTest {

    @Test
    fun `a tap event carries the testTag as name, the given screen, and the given ts`() {
        val event = tapEvent(testTag = "pay_now_cta", screen = "PaymentScreen", ts = 1_784_536_440_000L)

        assertEquals(
            AppEvent.Tap(name = "pay_now_cta", ts = 1_784_536_440_000L, screen = "PaymentScreen"),
            event,
        )
    }
}
