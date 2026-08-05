package com.vyaparpay.feature.payments

import android.view.View
import android.view.ViewGroup
import androidx.activity.ComponentActivity
import androidx.compose.material3.Text
import androidx.compose.ui.node.RootForTest
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import com.vyaparpay.core.screencontext.UiTreeCollector
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * The half of [UiTreeCollector.stop]'s contract that `UiTreeCollectorTest`
 * (`:core:screencontext`) cannot reach: the last host leaving must drop every
 * tracked root.
 *
 * **Why this lives in `:feature:payments`.** Attaching a root needs a real
 * [RootForTest], which needs a live Compose composition, which needs
 * Robolectric — none of which `:core:screencontext` has (a `SemanticsOwner`
 * has no constructor reachable from a plain JVM, the constraint
 * `UiTreeCollectorTest`'s own kdoc documents). This module already hosts the
 * collector's real-root tests for exactly that reason; see
 * `UiTreeCollectorPaymentScreenCanaryTest`'s kdoc for the dependency-direction
 * argument in full.
 *
 * **Why it is not an Activity-lifecycle test.** It drives [UiTreeCollector]
 * directly, with a collector it constructs itself. `ActivityScenario.recreate()`
 * uses a strict destroy-then-create ordering and provably cannot reproduce the
 * overlapping-host condition this contract exists for, so testing through an
 * Activity would prove less, not more. The activity here is only a host for a
 * real composition.
 *
 * **The regression.** `stop()` used to dispose the observer and cancel the
 * debounce but leave `attachedRoots` untouched. That was harmless while the
 * collector died with its Activity. Once it became the `@Singleton` it always
 * claimed to be (Phase-4 T8e), every destroyed Activity's `AndroidComposeView`
 * — and through its Context, the Activity and its whole semantics tree —
 * stayed reachable from an app-lifetime object, one per rotation, dark-mode
 * toggle, font-size or locale change, and every later capture walked the dead
 * roots as well.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class UiTreeCollectorRootLifecycleTest {

    @get:Rule
    val composeTestRule = createAndroidComposeRule<ComponentActivity>()

    @Test
    fun `the last host's stop drops every tracked root`() {
        composeTestRule.setContent { Text("a real composition, so there is a real RootForTest") }
        composeTestRule.waitForIdle()

        val root = findRootForTest(composeTestRule.activity.window.decorView)
            ?: error("No RootForTest in the rendered view tree -- see UiTreeCollectorPaymentScreenCanaryTest's kdoc.")

        // Dispatchers.Unconfined: the collector's internal `scope.launch`
        // bodies run inline on the calling thread, so both the attach and the
        // stop below are complete by the time each call returns.
        val collector = UiTreeCollector(scope = CoroutineScope(Dispatchers.Unconfined))
        collector.attachRoot(root)

        assertEquals(
            "precondition: attachRoot must track the window and capture it immediately",
            1,
            runBlocking { collector.tree.first() }.roots.size,
        )

        collector.stop()
        // A capture taken AFTER the stop is what reveals whether the root is
        // still tracked -- `stop()` deliberately does not emit one of its own.
        collector.forceImmediateCapture()

        assertEquals(
            "stop() must drop every tracked root; a retained root pins the whole destroyed " +
                "Activity in an app-lifetime @Singleton and gets walked by every later capture",
            0,
            runBlocking { collector.tree.first() }.roots.size,
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
