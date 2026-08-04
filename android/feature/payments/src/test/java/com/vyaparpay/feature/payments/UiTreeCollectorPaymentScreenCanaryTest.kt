package com.vyaparpay.feature.payments

import android.view.View
import android.view.ViewGroup
import androidx.activity.ComponentActivity
import androidx.compose.ui.node.RootForTest
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import com.vyaparpay.core.screencontext.RawSemanticsNode
import com.vyaparpay.core.screencontext.ScreenComponent
import com.vyaparpay.core.screencontext.SemanticSnapshotBuilder
import com.vyaparpay.core.screencontext.UiTreeCollector
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * The CI canary docs/07-ui-semantic-context.md §2.1's "honesty note" calls
 * for: `RootForTest`/`unmergedRootSemanticsNode` is test-oriented API used in
 * production ([UiTreeCollector]'s own kdoc explains the risk in full). This
 * test walks a REAL, rendered [PaymentScreen] — the actual production
 * composable, not a hand-built fixture — end to end through
 * [UiTreeCollector] and then [SemanticSnapshotBuilder], so a future Compose
 * upgrade that breaks either cast, or silently changes which semantics
 * properties a `TextField`/`AlertDialog`/`Snackbar` sets, fails this test
 * loudly instead of surfacing as a quietly wrong IR in production.
 *
 * **Why this test lives in `:feature:payments`, not `:core:screencontext`.**
 * `UiTreeCollector` is the class under test, but exercising it against a real
 * *screen* needs a real screen, and docs/03 §1's module dependency rule is
 * `:core:* -> :core:*` only — `:core:screencontext` cannot depend on
 * `:feature:payments`, even in a test-only configuration, without inverting
 * the architecture's whole dependency direction. `:feature:payments` already
 * depends on `:core:screencontext` (`implementation(project(":core:screencontext"))`
 * in its own `build.gradle.kts`), so hosting the canary here is the one
 * placement that needs no new, dependency-rule-bending edge.
 *
 * **Why `attachRoot` is called directly, not via `ViewRootForTest.Companion.
 * onViewCreatedCallback`.** That callback is a process-wide singleton var —
 * `createAndroidComposeRule` already claims it internally to build its own
 * root registry (needed for `onNodeWithTag`, `waitForIdle`, etc.), and a
 * second writer would clobber it, breaking the compose-ui-test framework
 * itself mid-test. Walking `activity.window.decorView` for the `RootForTest`
 * instance Compose already created and handing it to [UiTreeCollector.attachRoot]
 * directly sidesteps that conflict — see [UiTreeCollector]'s own kdoc for the
 * full reasoning, which is the same reasoning this test is built around.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class UiTreeCollectorPaymentScreenCanaryTest {

    @get:Rule
    val composeTestRule = createAndroidComposeRule<ComponentActivity>()

    @Test
    fun `UiTreeCollector walks the real PaymentScreen and SemanticSnapshotBuilder produces the expected components`() {
        composeTestRule.setContent {
            PaymentScreen(
                state = PaymentUiState(amountInput = "245"),
                events = InMemoryEventTracker(),
                onAmountChanged = {},
                onPayNowClicked = {},
                onDismissDialog = {},
                onSnackbarShown = {},
            )
        }
        composeTestRule.waitForIdle()

        val root = findRootForTest(composeTestRule.activity.window.decorView)
            ?: error(
                "No RootForTest found in the rendered view tree -- the RootForTest " +
                    "cast this canary exists to protect appears to have broken. If " +
                    "this test starts failing after a Compose version bump, that is " +
                    "the signal docs/07 §2.1's honesty note warns about, not a flaky test.",
            )

        // Dispatchers.Unconfined: runs UiTreeCollector's internal `scope.launch`
        // bodies inline on the calling thread, so attachRoot's capture is
        // synchronous by the time this call returns -- no extra pumping needed
        // in a Robolectric test that has no coroutines-android Main dispatcher.
        val collector = UiTreeCollector(scope = CoroutineScope(Dispatchers.Unconfined))
        collector.attachRoot(root)

        val tree = runBlocking { collector.tree.first() }

        // First proof: the raw walk itself found the real testTags docs/03
        // §4's convention puts on this screen -- the cast worked, and the
        // semantics properties this task's UiTreeCollector reads (TestTag,
        // EditableText, Role, OnClick, ...) came back populated from a real
        // composition, not just from a hand-built RawSemanticsNode fixture.
        val testTags = tree.roots.flatMap { allTestTags(it) }
        assertTrue("amount_input" in testTags)
        assertTrue("recipient_row" in testTags)
        assertTrue(PAY_NOW_TEST_TAG in testTags)

        // Second, stronger proof: the FULL pipeline -- UiTreeCollector's
        // walk, then SemanticSnapshotBuilder's transform -- produces the
        // expected screen_context/v1 components from that real tree.
        val ir = SemanticSnapshotBuilder.build(
            tree = tree,
            screen = "PaymentScreen",
            flow = "vendor_payment",
            recentEvents = emptyList(),
        )
        val amount = ir.components.filterIsInstance<ScreenComponent.AmountField>().singleOrNull()
        assertTrue("expected an amount_field component, got roles ${ir.components.map { it.role }}", amount != null)
        assertTrue(amount!!.value.endsWith("245"))
        val recipient = ir.components.filterIsInstance<ScreenComponent.Recipient>().singleOrNull()
        assertTrue("expected a recipient component", recipient != null)
        val cta = ir.components.filterIsInstance<ScreenComponent.PrimaryCta>().singleOrNull()
        assertTrue("expected an enabled primary_cta (canSubmit=true for amountInput=245)", cta?.enabled == true)
    }

    private fun allTestTags(node: RawSemanticsNode): List<String> =
        listOfNotNull(node.testTag) + node.children.flatMap { allTestTags(it) }

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
