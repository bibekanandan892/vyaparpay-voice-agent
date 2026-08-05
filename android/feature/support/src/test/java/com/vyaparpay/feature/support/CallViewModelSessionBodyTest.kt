package com.vyaparpay.feature.support

import android.view.View
import android.view.ViewGroup
import androidx.activity.ComponentActivity
import androidx.compose.runtime.Composable
import androidx.compose.ui.node.RootForTest
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import com.vyaparpay.core.analytics.AppEvent
import com.vyaparpay.core.analytics.EventTracker
import com.vyaparpay.core.analytics.RingBufferEventTracker
import com.vyaparpay.core.network.SessionCreateRequestDto
import com.vyaparpay.core.screencontext.AppStateManager
import com.vyaparpay.core.screencontext.NavigationTracker
import com.vyaparpay.core.screencontext.UiTreeCollector
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * The proof that [CallViewModel] does not fabricate its session body.
 *
 * `CallViewModelTest` asserts the request equals [AppStateManager.sessionCreateBody]'s
 * output, but does so against a manager with no capture attached — where that
 * output is the empty-but-honest `(userId, null, [])`. A `CallViewModel` that
 * simply constructed that triple itself would pass that test. This one closes
 * the hole by driving a **real capture through a real composition** first, so
 * the expected body carries a screen IR and a live event timeline that nothing
 * in `:feature:support` could invent.
 *
 * Why that matters beyond pedantry: `AppStateManager` is where capture
 * exclusion lives (docs/07 §2.1) — it retains the last *operational* screen
 * and refuses to overwrite it while the merchant sits on `HelpScreen`. A
 * session body assembled anywhere else would defeat that silently, and the
 * agent would answer the call looking at the help screen instead of the
 * payment that failed. This test is what makes "built from `AppStateManager`"
 * a checked claim rather than a code-review observation.
 *
 * Follows `UiTreeCollectorPaymentScreenCanaryTest`'s (`:feature:payments`)
 * setup for reaching a real [RootForTest] out of a Robolectric-rendered
 * composition — see that test's kdoc for why `attachRoot` is called directly
 * rather than through `ViewRootForTest.onViewCreatedCallback`.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class CallViewModelSessionBodyTest {

    @get:Rule
    val composeTestRule = createAndroidComposeRule<ComponentActivity>()

    @Test
    fun `the session request carries the real captured screen and event timeline`() {
        // Dispatchers.Unconfined throughout: UiTreeCollector's internal
        // launches and AppStateManager's eagerly-started combine both run
        // inline, so the capture below is visible synchronously.
        val scope = CoroutineScope(Dispatchers.Unconfined)
        val events = RingBufferEventTracker()
        val collector = UiTreeCollector(scope)
        val appState = AppStateManager(
            uiTreeCollector = collector,
            navigationTracker = NavigationTracker(events),
            eventTracker = events,
            scope = scope,
        )

        // Recorded BEFORE the capture: AppStateManager pulls eventTracker
        // .recent() inside its combine, at the moment a new tree arrives.
        events.record(AppEvent.Tap(name = "Pay Now", ts = 1_723_000_000_000L, screen = "PaymentScreen"))

        composeTestRule.setContent {
            OperationalScreenStandIn(events = events)
        }
        composeTestRule.waitForIdle()

        val root = findRootForTest(composeTestRule.activity.window.decorView)
            ?: error("No RootForTest in the rendered view tree -- see UiTreeCollectorPaymentScreenCanaryTest's kdoc.")
        collector.attachRoot(root)

        val launcher = FakeVoiceCallLauncher()
        CallViewModel(appState, launcher).startCall()

        val sent = Json.decodeFromString(SessionCreateRequestDto.serializer(), launcher.started.single())

        // 1. Byte-for-byte the manager's own body. Anything assembled inside
        //    CallViewModel would have to reproduce a live capture to match.
        assertEquals(appState.sessionCreateBody(CallViewModel.DEMO_USER_ID), sent)

        // 2. A real screen IR travelled: `screenContext` is null until an
        //    operational capture lands, so a non-null one cannot be faked from
        //    inside this module -- CallViewModel has no other source of a
        //    ScreenContextIr.
        val screenContext = sent.screenContext
        assertNotNull("screenContext must carry the captured screen, not null", screenContext)
        assertTrue("the IR should carry the screen_context/v1 envelope", "v" in screenContext!!)
        assertTrue("the IR should carry a components array", "components" in screenContext)

        // 3. The event timeline travelled too, and it is the one recorded on
        //    the shared EventTracker above -- the other half of what
        //    AppStateManager aggregates.
        val tap = sent.recentEvents.single { it.name == "Pay Now" }
        assertEquals("tap", tap.type)
        assertEquals(1_723_000_000_000L, tap.ts)
    }

    /**
     * Stands in for the operational screen the merchant was stuck on when they
     * went looking for help.
     *
     * A stand-in rather than a real `PaymentScreen`: that composable lives in
     * `:feature:payments`, and a `:feature:support -> :feature:payments` edge
     * is exactly what docs/03 §1 forbids (the rule task in the root build
     * script fails the build on it, test configurations excluded or not, since
     * this would need a production-shaped dependency to compile). Built from
     * this module's own [SupportButton] so it is still a real production
     * composable being walked, not a hand-built semantics fixture.
     */
    @Composable
    private fun OperationalScreenStandIn(events: EventTracker) {
        SupportButton(
            label = "Pay Now",
            testTag = "pay_now_cta",
            filled = true,
            events = events,
            onClick = {},
        )
    }

    private fun findRootForTest(view: View): RootForTest? {
        if (view is RootForTest) return view
        if (view is ViewGroup) {
            for (i in 0 until view.childCount) {
                findRootForTest(view.getChildAt(i))?.let { return it }
            }
        }
        return null
    }
}
