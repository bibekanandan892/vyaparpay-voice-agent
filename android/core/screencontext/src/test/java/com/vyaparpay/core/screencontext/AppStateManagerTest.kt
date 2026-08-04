package com.vyaparpay.core.screencontext

import com.vyaparpay.core.analytics.AppEvent
import com.vyaparpay.core.analytics.EventTracker
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [AppStateManager]'s combine (Phase-4 T7b): the right [AppContextState] from
 * fake inputs, [AppStateManager.sessionCreateBody]'s wire mapping, and
 * capture exclusion (docs/07 §2.1) retaining the last operational screen.
 *
 * **Fakes, per this codebase's established conventions.** [EventTracker] is
 * an interface, so a local private fake (same shape as
 * `NavigationTrackerTest`'s own `FakeEventTracker`). [NavigationTracker] is a
 * concrete class with no dependencies of its own to fake; it is driven
 * directly through its testable `onDestinationChanged` core, exactly as
 * `NavigationTrackerTest` does. `UiTreeCollector` is deliberately NOT
 * constructed here at all: [AppStateManager]'s own kdoc (judgment call 1b)
 * explains why its tree dependency is typed as a bare `Flow<RawSemanticsTree>`
 * — a real `UiTreeCollector` cannot be fed fixture content on a plain JVM
 * (`UiTreeCollectorTest`'s own kdoc), so a `MutableStateFlow<RawSemanticsTree>`
 * stands in for it here, which is the actual fake this test needs.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class AppStateManagerTest {

    private class FakeEventTracker : EventTracker {
        val recorded = mutableListOf<AppEvent>()
        private val _events = MutableSharedFlow<AppEvent>(extraBufferCapacity = 16)

        override fun record(event: AppEvent) {
            recorded += event
            _events.tryEmit(event)
        }

        override fun recent(count: Int): List<AppEvent> = recorded.takeLast(count).asReversed()
        override val lastAction: AppEvent? get() = recorded.lastOrNull()
        override val eventStream: SharedFlow<AppEvent> = _events.asSharedFlow()
    }

    /** One `primary_cta`-mappable node — enough to prove the builder actually ran. */
    private fun payNowCtaTree(): RawSemanticsTree = RawSemanticsTree(
        roots = listOf(
            RawSemanticsNode(
                id = 1,
                testTag = "pay_now_cta",
                role = RawRole.BUTTON,
                textRuns = listOf("Pay Now"),
                enabled = true,
                hasOnClick = true,
            ),
        ),
    )

    private fun emptyTree(): RawSemanticsTree = RawSemanticsTree(roots = emptyList())

    // ------------------------------------------------------------------
    // The combine
    // ------------------------------------------------------------------

    @Test
    fun `the combine builds a screen IR for an operational route`() = runTest {
        val tree = MutableStateFlow(payNowCtaTree())
        val eventTracker = FakeEventTracker()
        val navigationTracker = NavigationTracker(eventTracker)
        val manager = AppStateManager(tree, navigationTracker, eventTracker, backgroundScope)

        navigationTracker.onDestinationChanged("PaymentScreen")
        runCurrent()

        val state = manager.state.value
        assertEquals("PaymentScreen", state.route)
        assertEquals("vendor_payment", state.flow)
        val screen = requireNotNull(state.screen)
        assertEquals("PaymentScreen", screen.screen)
        assertEquals("vendor_payment", screen.flow)
        val cta = screen.components.single() as ScreenComponent.PrimaryCta
        assertEquals("Pay Now", cta.label)
        assertTrue(cta.enabled)
    }

    @Test
    fun `a new capture on the same route replaces the retained screen`() = runTest {
        val tree = MutableStateFlow(emptyTree())
        val eventTracker = FakeEventTracker()
        val navigationTracker = NavigationTracker(eventTracker)
        val manager = AppStateManager(tree, navigationTracker, eventTracker, backgroundScope)

        navigationTracker.onDestinationChanged("PaymentScreen")
        runCurrent()
        assertTrue(requireNotNull(manager.state.value.screen).components.isEmpty())

        tree.value = payNowCtaTree()
        runCurrent()

        assertEquals(1, requireNotNull(manager.state.value.screen).components.size)
    }

    // ------------------------------------------------------------------
    // Stale-tree guard (audit fix, 2026-08-04)
    // ------------------------------------------------------------------

    @Test
    fun `a route change with no new capture never relabels the old screen's components as the new route`() =
        runTest {
            val tree = MutableStateFlow(payNowCtaTree())
            val eventTracker = FakeEventTracker()
            val navigationTracker = NavigationTracker(eventTracker)
            val manager = AppStateManager(tree, navigationTracker, eventTracker, backgroundScope)

            navigationTracker.onDestinationChanged("PaymentScreen")
            runCurrent()
            assertEquals("PaymentScreen", requireNotNull(manager.state.value.screen).screen)

            // Navigate WITHOUT a new capture -- exactly what a real
            // NavController does: `route` flips synchronously inside
            // onDestinationChanged, while `tree` still holds PaymentScreen's
            // capture until UiTreeCollector's debounce fires.
            navigationTracker.onDestinationChanged("SettlementsScreen")
            runCurrent()

            val midNav = manager.state.value
            // route/flow track the live nav position...
            assertEquals("SettlementsScreen", midNav.route)
            // ...but the IR must still be the last one actually captured. Before
            // the fix this read "SettlementsScreen" while carrying PaymentScreen's
            // pay_now_cta -- a screen the merchant was never on, which
            // ScreenContextPublisher then shipped as a full ctx.snapshot.
            val retained = requireNotNull(midNav.screen)
            assertEquals("PaymentScreen", retained.screen)
            assertEquals("vendor_payment", retained.flow)

            // The real capture lands -> now, and only now, the IR advances.
            tree.value = emptyTree()
            runCurrent()

            val settled = requireNotNull(manager.state.value.screen)
            assertEquals("SettlementsScreen", settled.screen)
            assertTrue(settled.components.isEmpty())
        }

    @Test
    fun `leaving an excluded route does not build the new route's IR out of the excluded screen's capture`() =
        runTest {
            val tree = MutableStateFlow(payNowCtaTree())
            val eventTracker = FakeEventTracker()
            val navigationTracker = NavigationTracker(eventTracker)
            val manager = AppStateManager(tree, navigationTracker, eventTracker, backgroundScope)

            navigationTracker.onDestinationChanged("PaymentScreen")
            runCurrent()

            // On HelpScreen a capture DOES arrive; it is excluded from being
            // built, but it must still be marked as seen.
            navigationTracker.onDestinationChanged("HelpScreen")
            runCurrent()
            tree.value = emptyTree() // HelpScreen's own capture
            runCurrent()

            // Leaving Help for an operational route, still on Help's capture.
            // Tracking `lastSeenTree` only on builds would make Help's tree look
            // "new" here and yield a DashboardScreen IR built from HelpScreen's
            // components -- the excluded screen's content leaking out under an
            // operational label, the one thing capture exclusion exists to stop.
            navigationTracker.onDestinationChanged("DashboardScreen")
            runCurrent()

            val retained = requireNotNull(manager.state.value.screen)
            assertEquals("DashboardScreen", manager.state.value.route)
            assertEquals("PaymentScreen", retained.screen)
            assertEquals(1, retained.components.size)
        }

    // ------------------------------------------------------------------
    // Capture exclusion (docs/07 §2.1)
    // ------------------------------------------------------------------

    @Test
    fun `capture exclusion retains the last operational screen while on an excluded route`() = runTest {
        val tree = MutableStateFlow(payNowCtaTree())
        val eventTracker = FakeEventTracker()
        val navigationTracker = NavigationTracker(eventTracker)
        val manager = AppStateManager(tree, navigationTracker, eventTracker, backgroundScope)

        navigationTracker.onDestinationChanged("PaymentScreen")
        runCurrent()
        val operationalScreen = requireNotNull(manager.state.value.screen)
        assertEquals("PaymentScreen", operationalScreen.screen)

        navigationTracker.onDestinationChanged("HelpScreen")
        runCurrent()

        val stateOnHelp = manager.state.value
        // route/flow track the LIVE nav position...
        assertEquals("HelpScreen", stateOnHelp.route)
        assertEquals("support", stateOnHelp.flow)
        // ...but the retained IR is untouched: still PaymentScreen.
        assertEquals(operationalScreen, stateOnHelp.screen)
    }

    @Test
    fun `both support surfaces are excluded from capture`() = runTest {
        val tree = MutableStateFlow(payNowCtaTree())
        val eventTracker = FakeEventTracker()
        val navigationTracker = NavigationTracker(eventTracker)
        val manager = AppStateManager(tree, navigationTracker, eventTracker, backgroundScope)

        navigationTracker.onDestinationChanged("PaymentScreen")
        runCurrent()
        val operationalScreen = manager.state.value.screen

        navigationTracker.onDestinationChanged("ConversationOverlay")
        runCurrent()

        assertEquals(operationalScreen, manager.state.value.screen)
    }

    @Test
    fun `a cold start on an excluded route captures nothing`() = runTest {
        val tree = MutableStateFlow(payNowCtaTree())
        val eventTracker = FakeEventTracker()
        val navigationTracker = NavigationTracker(eventTracker)
        val manager = AppStateManager(tree, navigationTracker, eventTracker, backgroundScope)

        navigationTracker.onDestinationChanged("HelpScreen")
        runCurrent()

        assertNull(manager.state.value.screen)
    }

    @Test
    fun `isExcludedFromCapture matches docs 07 section 2_1's excluded set`() {
        assertTrue(AppStateManager.isExcludedFromCapture("HelpScreen", "support"))
        assertTrue(AppStateManager.isExcludedFromCapture("ConversationOverlay", "support"))
        assertFalse(AppStateManager.isExcludedFromCapture("PaymentScreen", "vendor_payment"))
        assertFalse(AppStateManager.isExcludedFromCapture("DashboardScreen", "home"))
    }

    // ------------------------------------------------------------------
    // sessionCreateBody
    // ------------------------------------------------------------------

    @Test
    fun `sessionCreateBody carries a null screenContext until something operational is captured`() = runTest {
        val tree = MutableStateFlow(payNowCtaTree())
        val eventTracker = FakeEventTracker()
        val navigationTracker = NavigationTracker(eventTracker)
        val manager = AppStateManager(tree, navigationTracker, eventTracker, backgroundScope)

        navigationTracker.onDestinationChanged("HelpScreen")
        runCurrent()

        val body = manager.sessionCreateBody("usr_rajesh01")
        assertEquals("usr_rajesh01", body.userId)
        assertNull(body.screenContext)
    }

    @Test
    fun `sessionCreateBody maps the retained screen and recent events onto the wire shape`() = runTest {
        val tree = MutableStateFlow(payNowCtaTree())
        val eventTracker = FakeEventTracker()
        val navigationTracker = NavigationTracker(eventTracker)
        val manager = AppStateManager(tree, navigationTracker, eventTracker, backgroundScope)

        // Recorded before the triggering nav change so it's already in
        // EventTracker.recent() the moment the combine reads it.
        eventTracker.record(AppEvent.Tap(name = "pay_now_cta", ts = 1_784_536_440_000L, screen = "PaymentScreen"))
        navigationTracker.onDestinationChanged("PaymentScreen")
        runCurrent()

        val body = manager.sessionCreateBody("usr_rajesh01")
        val screenContext = requireNotNull(body.screenContext)
        assertEquals("PaymentScreen", screenContext["screen"]!!.jsonPrimitive.content)
        assertEquals("screen_context/v1", screenContext["v"]!!.jsonPrimitive.content)

        val tapDto = body.recentEvents.first { it.name == "pay_now_cta" }
        assertEquals("tap", tapDto.type)
        assertEquals(1_784_536_440_000L, tapDto.ts)
    }

    // ------------------------------------------------------------------
    // events (judgment call 2's forwarding half)
    // ------------------------------------------------------------------

    @Test
    fun `events forwards EventTracker's live stream`() = runTest {
        val tree = MutableStateFlow(emptyTree())
        val eventTracker = FakeEventTracker()
        val navigationTracker = NavigationTracker(eventTracker)
        val manager = AppStateManager(tree, navigationTracker, eventTracker, backgroundScope)

        val received = mutableListOf<AppEvent>()
        backgroundScope.launch { manager.events.collect { received += it } }
        runCurrent()

        val tap = AppEvent.Tap(name = "pay_now_cta", ts = 1L, screen = "PaymentScreen")
        eventTracker.record(tap)
        runCurrent()

        assertEquals(listOf(tap), received)
    }
}
