package com.vyaparpay

import android.os.Looper
import androidx.activity.compose.setContent
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextInput
import com.vyaparpay.core.screencontext.RawSemanticsNode
import com.vyaparpay.core.screencontext.RawSemanticsTree
import com.vyaparpay.core.screencontext.ScreenComponentRole
import com.vyaparpay.core.screencontext.SemanticSnapshotBuilder
import com.vyaparpay.core.ui.theme.VyaparTheme
import com.vyaparpay.feature.payments.PaymentDestination
import com.vyaparpay.feature.payments.PaymentRoute
import dagger.hilt.android.testing.HiltAndroidRule
import dagger.hilt.android.testing.HiltAndroidTest
import dagger.hilt.android.testing.HiltTestApplication
import java.time.Duration
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

/**
 * Phase-4 T8f's proof: the "Daily Limit Exceeded" decline dialog — a Compose
 * `AlertDialog` living in its own Android window — reaches the
 * `screen_context/v1` IR because [ChildWindowTracker] *discovered* that
 * window, with nothing in the test doing the discovering.
 *
 * **What makes this different from `UiTreeCollectorPaymentScreenCanaryTest`'s
 * dialog test (`:feature:payments`), which asserts a similar-looking thing.**
 * That test reaches the dialog through `ShadowDialog.getLatestDialog()` and
 * calls `collector.attachRoot(dialogRoot)` itself. It proves the *collector*
 * can walk a dialog window once someone hands it one — which was exactly the
 * gap T8f exists to close, because in production nobody ever did. Here the
 * test never names the dialog's window, never calls `attachRoot`, and never
 * touches Robolectric's dialog shadows: it types an amount, taps Pay Now, and
 * then reads what the REAL [MainActivity]'s REAL Hilt-injected
 * `UiTreeCollector` captured. If the production discovery mechanism does
 * nothing, `tree.roots` stays at one and every assertion below fails.
 *
 * **Why the activity's content is swapped instead of navigated to.**
 * `AppNavHost` currently has no navigation edge from the start destination
 * (`DashboardScreen`) to `PaymentScreen` — the only edge in the whole graph is
 * `OrdersScreen -> OrderTrackingScreen` — so there is no sequence of taps that
 * gets a test from a launched [MainActivity] to the canonical decline screen.
 * `ComponentActivity.setContent` reuses the activity's existing `ComposeView`
 * (and therefore its existing `AndroidComposeView`), so the main-window root
 * [MainActivity.attachComposeRoot] already handed to the collector stays the
 * same instance and stays valid — the swap changes what the app is showing,
 * not any part of the machinery under test. Adding a nav edge purely to make
 * this test reachable would have been a product change smuggled in through a
 * test, which is worse.
 *
 * The screen itself is the real one: `PaymentRoute` -> `hiltViewModel()` ->
 * the real `@HiltViewModel PaymentViewModel` over `SeededPaymentRepository`,
 * so "245" really does exceed the seeded daily limit and really does produce
 * the canonical `DAILY_LIMIT_EXCEEDED` dialog (docs/01 §7 steps 1-2).
 */
@HiltAndroidTest
@RunWith(RobolectricTestRunner::class)
@Config(application = HiltTestApplication::class, sdk = [34])
class MainActivityDialogWindowCaptureTest {

    @get:Rule(order = 0)
    val hiltRule = HiltAndroidRule(this)

    @get:Rule(order = 1)
    val composeTestRule = createAndroidComposeRule<MainActivity>()

    @Test
    fun `the decline dialog's own window is discovered and its components reach the IR`() {
        showDeclineDialog()

        val tree = latestTree()
        assertEquals(
            "expected TWO attached roots -- the main activity window plus the AlertDialog's own " +
                "window. One root means ChildWindowTracker never discovered the dialog window, " +
                "which is precisely the production bug T8f exists to fix.",
            2,
            tree.roots.size,
        )

        // Content only a walk of the DIALOG's window could have produced: the
        // dismiss button lives inside the AlertDialog's subcomposition, in the
        // dialog's decorView, which `window.decorView` never reaches.
        val testTags = tree.roots.flatMap(::allTestTags)
        assertTrue(
            "expected the decline dialog's own dismiss CTA in the captured tree, got $testTags",
            DIALOG_DISMISS_TEST_TAG in testTags,
        )

        val ir = SemanticSnapshotBuilder.build(
            tree = tree,
            screen = PaymentDestination.ROUTE,
            flow = PaymentDestination.FLOW,
            recentEvents = emptyList(),
        )
        val dialog = ir.components.singleOrNull { it.role == ScreenComponentRole.DIALOG }
        assertTrue(
            "expected exactly one dialog component in the IR, roles were ${ir.components.map { it.role }}",
            dialog != null,
        )
        assertTrue(
            "expected the canonical decline dialog's own title, got '${dialog!!.label}'",
            DECLINE_DIALOG_TITLE in dialog.label,
        )
    }

    /**
     * The other half of the contract [UiTreeCollector]'s kdoc spells out:
     * `attachRoot` bypasses the debounce because a new window is information
     * the agent must not miss, and `detachRoot` deliberately does not, because
     * 300 ms of staleness on a window closing is imperceptible. Both halves
     * have to actually fire — an unbalanced `attachRoot` would leave the
     * app-lifetime `@Singleton` collector republishing a dialog that closed
     * minutes ago, which in a live support call is worse than no dialog at all.
     *
     * The 500 ms advance is what makes this test honest about which path it is
     * exercising: it is longer than the 300 ms trailing-edge debounce
     * `detachRoot` goes through, so the detached state has to arrive via the
     * ordinary debounced capture rather than an immediate one.
     */
    @Test
    fun `dismissing the dialog detaches its window and the dialog leaves the IR`() {
        showDeclineDialog()
        assertEquals("precondition: the dialog window is attached", 2, latestTree().roots.size)

        composeTestRule.onNodeWithTag(DIALOG_DISMISS_TEST_TAG).performClick()
        settle(millis = 500)

        val tree = latestTree()
        assertEquals(
            "expected the dialog's window to be reported detached once dismissed -- a tracked " +
                "root that is never detached accumulates in UiTreeCollector.attachedRoots and " +
                "keeps a dead window in every future snapshot.",
            1,
            tree.roots.size,
        )
        val ir = SemanticSnapshotBuilder.build(
            tree = tree,
            screen = PaymentDestination.ROUTE,
            flow = PaymentDestination.FLOW,
            recentEvents = emptyList(),
        )
        assertTrue(
            "expected no dialog component after dismissal, roles were ${ir.components.map { it.role }}",
            ir.components.none { it.role == ScreenComponentRole.DIALOG },
        )
    }

    /**
     * The teardown half of the same leak. `UiTreeCollector` is an app-lifetime
     * `@Singleton` — from the annotation on the class itself, not from a module
     * binding; `ScreenContextModule` provides only the `CoroutineScope` it
     * takes, and this parenthetical used to miscredit it (corrected in Phase-4
     * T8e, which is also when the annotation the sentence assumed was actually
     * added). It therefore outlives every
     * [MainActivity] instance a configuration change destroys and recreates —
     * a tracker that walked away while a dialog was still up would leave that
     * dialog's dead window in the singleton's `attachedRoots`, and the next
     * activity's very first capture would publish a window that no longer
     * exists.
     *
     * [ChildWindowTracker.stop] is called directly rather than through
     * `onDestroy`: it is the exact call [MainActivity.onDestroy] makes, and
     * driving a real destroy under `createAndroidComposeRule` would tear down
     * the rule's own composition before the assertion could read anything.
     */
    @Test
    fun `stopping the tracker detaches windows that are still open`() {
        showDeclineDialog()
        assertEquals("precondition: the dialog window is attached", 2, latestTree().roots.size)

        composeTestRule.runOnUiThread { composeTestRule.activity.childWindowTracker.stop() }
        settle(millis = 500)

        assertEquals(
            "ChildWindowTracker.stop() must report every window it is still tracking as " +
                "detached, or the app-lifetime UiTreeCollector keeps the dead window forever.",
            1,
            latestTree().roots.size,
        )
    }

    // -- Driving the canonical scenario ------------------------------------

    private fun showDeclineDialog() {
        composeTestRule.waitForIdle()
        composeTestRule.runOnUiThread {
            composeTestRule.activity.setContent { VyaparTheme { PaymentRoute() } }
        }
        composeTestRule.waitForIdle()

        composeTestRule.onNodeWithTag(AMOUNT_TEST_TAG).performTextInput(CANONICAL_AMOUNT)
        composeTestRule.onNodeWithTag(PAY_NOW_TEST_TAG).performClick()
        settle(millis = 500)
    }

    /**
     * Robolectric's main looper is PAUSED, so `waitForIdle()` alone runs only
     * what is already due. Advancing the clock afterwards is what lets both
     * [ChildWindowTracker]'s coalesced `Handler.post` scan and
     * `UiTreeCollector`'s 300 ms debounced capture actually run — the same
     * scheduling they use on a device, just driven deterministically.
     */
    private fun settle(millis: Long) {
        composeTestRule.waitForIdle()
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(millis))
        composeTestRule.waitForIdle()
    }

    private fun latestTree(): RawSemanticsTree = runBlocking {
        // withTimeout, not a bare first(): `tree` is a filterNotNull'd
        // StateFlow that simply never emits if capture regresses, and a hung
        // CI job names its own cause far worse than a failed assertion does.
        // Same reasoning MainActivityScreenContextTest documents at length.
        withTimeout(CAPTURE_TIMEOUT_MILLIS) {
            composeTestRule.activity.uiTreeCollector.tree.first()
        }
    }

    private fun allTestTags(node: RawSemanticsNode): List<String> =
        listOfNotNull(node.testTag) + node.children.flatMap(::allTestTags)

    private companion object {
        /**
         * testTags owned by `:feature:payments`, where they are `private`/
         * `internal` consts on `PaymentScreen.kt` and therefore not visible
         * from `:app`. Repeated as literals rather than widened for a test —
         * docs/03 §4's tag table is the shared contract they both encode, and
         * a drift here fails loudly with the tag list printed.
         */
        const val AMOUNT_TEST_TAG = "amount_input"
        const val PAY_NOW_TEST_TAG = "pay_now_cta"
        const val DIALOG_DISMISS_TEST_TAG = "daily_limit_dismiss_secondary"

        /** Rajesh's over-limit vendor payment (docs/01 §7 step 1). */
        const val CANONICAL_AMOUNT = "245"

        /** `PaymentViewModel`'s own title for `ApiError.DAILY_LIMIT_EXCEEDED`. */
        const val DECLINE_DIALOG_TITLE = "Daily Limit Exceeded"

        const val CAPTURE_TIMEOUT_MILLIS = 10_000L
    }
}
