package com.vyaparpay.core.screencontext

import com.vyaparpay.core.analytics.AppEvent
import com.vyaparpay.core.analytics.EventTracker
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * The docs/03 §3.8 route -> flow table and the `nav` event a destination
 * change fires — exercised through [NavigationTracker.onDestinationChanged],
 * the testable core decoupled from the real `NavController` (an Android
 * framework type this plain-JUnit module cannot easily fake).
 */
class NavigationTrackerTest {

    private class FakeEventTracker : EventTracker {
        val recorded = mutableListOf<AppEvent>()
        override fun record(event: AppEvent) {
            recorded += event
        }

        override fun recent(count: Int): List<AppEvent> = recorded.takeLast(count).asReversed()
        override val lastAction: AppEvent? get() = recorded.lastOrNull()
    }

    @Test
    fun `every docs 03 section 3_8 route maps to its documented flow`() {
        val tracker = NavigationTracker(FakeEventTracker())
        val cases = mapOf(
            "PaymentScreen" to "vendor_payment",
            "SettlementsScreen" to "settlements_review",
            "OrdersScreen" to "device_orders",
            "OrderTrackingScreen" to "device_orders",
            "DashboardScreen" to "home",
            "HelpScreen" to "support",
            "ConversationOverlay" to "support",
        )

        cases.forEach { (route, expectedFlow) ->
            assertEquals("route=$route", expectedFlow, tracker.flowFor(route))
        }
    }

    @Test
    fun `an unmapped route folds to the empty flow rather than crashing`() {
        val tracker = NavigationTracker(FakeEventTracker())

        assertEquals("", tracker.flowFor("SomeFutureScreen"))
    }

    @Test
    fun `a destination change updates route and flow`() {
        val tracker = NavigationTracker(FakeEventTracker(), clock = { 1_000L })

        tracker.onDestinationChanged("PaymentScreen")

        assertEquals("PaymentScreen", tracker.route.value)
        assertEquals("vendor_payment", tracker.flow.value)
    }

    @Test
    fun `a destination change fires a nav event carrying the previous route as from`() {
        val events = FakeEventTracker()
        val tracker = NavigationTracker(events, clock = { 1_784_536_452_000L })

        tracker.onDestinationChanged("DashboardScreen")
        tracker.onDestinationChanged("PaymentScreen")

        val navEvent = events.recorded.last() as AppEvent.Nav
        assertEquals("PaymentScreen", navEvent.name)
        assertEquals("DashboardScreen", navEvent.from)
        assertEquals(1_784_536_452_000L, navEvent.ts)
    }

    @Test
    fun `the first destination change carries the initial empty route as from`() {
        val events = FakeEventTracker()
        val tracker = NavigationTracker(events)

        tracker.onDestinationChanged("DashboardScreen")

        assertEquals("", (events.recorded.single() as AppEvent.Nav).from)
    }

    @Test
    fun `the support surfaces both map to the support flow`() {
        val tracker = NavigationTracker(FakeEventTracker())

        tracker.onDestinationChanged("HelpScreen")
        assertEquals("support", tracker.flow.value)

        tracker.onDestinationChanged("ConversationOverlay")
        assertEquals("support", tracker.flow.value)
    }
}
