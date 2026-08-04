package com.vyaparpay

import androidx.compose.ui.test.junit4.createAndroidComposeRule
import com.vyaparpay.core.screencontext.RawSemanticsNode
import com.vyaparpay.feature.dashboard.DashboardDestination
import dagger.hilt.android.testing.HiltAndroidRule
import dagger.hilt.android.testing.HiltAndroidTest
import dagger.hilt.android.testing.HiltTestApplication
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * The full end-to-end proof Phase-4 T8a's own brief calls for: the REAL
 * `@AndroidEntryPoint` [MainActivity] launched under a real Hilt graph (via
 * [HiltTestApplication]) -- the first test in this codebase that boots Hilt
 * and Compose together outside a feature-scoped canary. Every capture-
 * pipeline piece ([UiTreeCollector], [NavigationTracker], the Hilt bindings
 * that make both resolvable) was already unit-tested in isolation before this
 * task, but nothing proved the REAL, running app actually wires them together
 * -- [MainActivity]'s own kdoc documents six separate judgment calls this
 * class makes, and this one test verifies the empirically riskiest of them
 * (judgment call 5: the post-after-setContent `RootForTest` walk timing) as
 * a side effect of proving the whole pipeline end to end, rather than as a
 * second, narrower test: an earlier version of this file paired this test
 * with a lighter one driving a bare `ComponentActivity` directly, but
 * `:app`'s real, restrictive `AndroidManifest.xml` (only `.MainActivity` is
 * declared, matching docs/03's single-activity design) makes a bare
 * `ComponentActivity` unresolvable to Robolectric's `ActivityScenario`
 * inside this specific module -- confirmed empirically, not assumed -- so
 * that narrower test was dropped as duplicate coverage: everything it would
 * have proven, this test already proves against the real activity, with
 * higher fidelity.
 *
 * Verifies three separate things [MainActivity]'s kdoc claims, all at once,
 * against the same activity instance:
 *
 * 1. [MainActivity.uiTreeCollector] is a real, Hilt-injected
 *    `UiTreeCollector` (not null, not a stub) whose `.tree` actually captured
 *    `AppNavHost`'s real, rendered start destination (`DashboardScreen` --
 *    [DashboardDestination.ROOT_TEST_TAG] is real content only a genuine walk
 *    of the genuine Compose tree, discovered via the real
 *    `window.decorView.post { }` call in `MainActivity.onCreate`, could
 *    find).
 * 2. [MainActivity.navigationTracker] is a real, Hilt-injected
 *    `NavigationTracker` whose `.bind` call against the hoisted
 *    `NavHostController` actually fired -- `route`/`flow` reflect the real
 *    start destination, not the class's own unbound defaults (`""`/`""`).
 * 3. Both 1 and 2 read off the SAME `@Singleton CoroutineScope`
 *    (`ScreenContextModule`'s binding, `:core:screencontext`) without
 *    deadlocking or throwing "Module with the Main dispatcher is missing" --
 *    the concrete proof that `:core:screencontext`'s new
 *    `kotlinx-coroutines-android` dependency (see that module's
 *    `build.gradle.kts`) is doing real work, not just satisfying a
 *    compile-time symbol.
 */
@HiltAndroidTest
@RunWith(RobolectricTestRunner::class)
@Config(application = HiltTestApplication::class, sdk = [34])
class MainActivityScreenContextTest {

    @get:Rule(order = 0)
    val hiltRule = HiltAndroidRule(this)

    @get:Rule(order = 1)
    val composeTestRule = createAndroidComposeRule<MainActivity>()

    @Test
    fun `MainActivity wires UiTreeCollector and NavigationTracker to the real running app`() {
        composeTestRule.waitForIdle()

        val activity = composeTestRule.activity

        val tree = runBlocking { activity.uiTreeCollector.tree.first() }
        val testTags = tree.roots.flatMap { allTestTags(it) }
        assertTrue(
            "expected the real DashboardScreen's root testTag in the captured tree, " +
                "got $testTags -- MainActivity's post-after-setContent RootForTest walk " +
                "did not reach the real Compose content",
            DashboardDestination.ROOT_TEST_TAG in testTags,
        )

        assertTrue(
            "NavigationTracker.bind(navController) should have fired for the real start " +
                "destination once bound inside MainActivity's LaunchedEffect(Unit)",
            activity.navigationTracker.route.value == DashboardDestination.ROUTE,
        )
        assertTrue(
            "NavigationTracker.flow should have resolved from the real bound route",
            activity.navigationTracker.flow.value == DashboardDestination.FLOW,
        )
    }

    private fun allTestTags(node: RawSemanticsNode): List<String> =
        listOfNotNull(node.testTag) + node.children.flatMap { allTestTags(it) }
}
