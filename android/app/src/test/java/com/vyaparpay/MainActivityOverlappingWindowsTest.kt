package com.vyaparpay

import android.os.Looper
import androidx.activity.compose.setContent
import com.vyaparpay.core.screencontext.AppStateManager
import com.vyaparpay.core.screencontext.RawSemanticsNode
import com.vyaparpay.core.ui.theme.VyaparTheme
import com.vyaparpay.feature.support.SupportDestination
import com.vyaparpay.feature.support.SupportRoute
import dagger.hilt.android.EntryPointAccessors
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
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

/**
 * Two live `MainActivity` instances at once — the overlap that reference
 * counting `UiTreeCollector.start()` made survivable, and which therefore needs
 * its own coverage for the first time.
 *
 * Reachable in production from `AndroidCallNotifier.contentIntent()`, which
 * relaunches this activity `FLAG_ACTIVITY_NEW_TASK or FLAG_ACTIVITY_CLEAR_TOP`
 * against a `standard` launchMode — the merchant tapping the ongoing-call
 * notification to get back to the app. (Note: a *configuration change* is
 * destroy-then-create and does NOT produce this overlap; see
 * [ChildWindowTracker]'s companion kdoc.)
 *
 * `Robolectric.buildActivity` is used directly rather than
 * `createAndroidComposeRule`, because the rule owns exactly one activity and
 * the whole subject here is the second one.
 */
@HiltAndroidTest
@RunWith(RobolectricTestRunner::class)
@Config(application = HiltTestApplication::class, sdk = [34])
class MainActivityOverlappingWindowsTest {

    @get:Rule(order = 0)
    val hiltRule = HiltAndroidRule(this)

    /**
     * The HIGH fix: each Activity's [ChildWindowTracker] must adopt only its own
     * windows.
     *
     * Before the fix, the scan excluded only its own `hostView`, so with two
     * live activities each tracker adopted the *other's* decorView — four roots
     * for two windows, every component duplicated, and the dying Activity's
     * entire screen flat-mapped into the next snapshot (`SemanticSnapshotBuilder`
     * does `tree.roots.flatMap { merge(it) }` with no dedup). Two roots is the
     * correct count: one main window per live Activity, each attached by its own
     * `MainActivity.attachComposeRoot`.
     */
    @Test
    fun `each activity's tracker adopts only its own window during overlap`() {
        val a = Robolectric.buildActivity(MainActivity::class.java).setup()
        settle()
        assertEquals("precondition: one activity, one root", 1, rootCount(a.get()))

        val b = Robolectric.buildActivity(MainActivity::class.java).setup()
        settle()

        assertEquals(
            "the two activities share one @Singleton UiTreeCollector",
            a.get().uiTreeCollector,
            b.get().uiTreeCollector,
        )
        assertEquals(
            "two live activities own two windows between them. Four means each tracker adopted " +
                "the other's decorView, which duplicates every component and flat-maps the " +
                "outgoing Activity's whole screen into the next snapshot.",
            2,
            rootCount(b.get()),
        )

        a.destroy()
        settle()
        assertEquals(
            "the destroyed activity's window must be gone once it is destroyed",
            1,
            rootCount(b.get()),
        )
    }

    /**
     * The end-to-end consequence the review asked to be demonstrated rather than
     * read from source, and it **reproduces**: while two activities overlap, the
     * outgoing one's support screen is captured and published under the incoming
     * one's non-excluded route.
     *
     * `AppStateManager` gates capture on route/flow only
     * (`isExcludedFromCapture(route, flow)`); it never asks which window content
     * came from. `NavigationTracker` is a `@Singleton`, so the incoming
     * Activity's `bind` reports its own start destination — `DashboardScreen`,
     * which is not excluded — while the outgoing Activity's `HelpScreen` window
     * is still attached to the shared collector and still walked by every
     * capture.
     *
     * **This is NOT fixed by the `ChildWindowTracker` change above, and is
     * deliberately left unfixed here.** The cross-adoption fix removes the
     * duplication; it cannot remove this, because both windows are attached
     * legitimately — each by its own Activity's `attachComposeRoot`. Closing it
     * properly means `UiTreeCollector` tracking roots per host and capturing only
     * the foreground host's, which is a design change to a class two other tasks
     * depend on, not something to smuggle into a review fix. Recorded here as a
     * failing-by-assertion record of real behaviour so it cannot be lost: the
     * assertion below pins what the code ACTUALLY does today, and the kdoc says
     * plainly that what it does is wrong.
     */
    @Test
    fun `KNOWN GAP - an overlapping support screen is published under the incoming non-excluded route`() {
        val appState = EntryPointAccessors.fromApplication(
            org.robolectric.RuntimeEnvironment.getApplication(),
            ScreenContextCanaryEntryPoint::class.java,
        ).appStateManager()

        val a = Robolectric.buildActivity(MainActivity::class.java).setup()
        settle()
        // Put the outgoing activity on the support surface, the way a merchant
        // on a call would be.
        //
        // Swapping A's content shows the support surface WITHOUT navigating, so
        // the shared route stays whatever A's own bind set -- DashboardScreen --
        // for the whole test. An earlier version of this comment claimed the
        // incoming activity's bind creates the asymmetry; it does not, and
        // `isExcludedFromCapture` is never exercised here. This is therefore a
        // simplification of the production sequence (A navigates to HelpScreen,
        // route becomes excluded, B relaunches and flips it back to a
        // non-excluded one). What it pins is narrower than that sequence and
        // still exactly the gap: support content reaching a snapshot labelled
        // with a non-excluded route.
        a.get().runOnUiThread { a.get().setContent { VyaparTheme { SupportRoute() } } }
        settle()

        val b = Robolectric.buildActivity(MainActivity::class.java).setup()
        settle()

        val state = appState.state.value
        val tags = latestTree(b.get()).roots.flatMap(::allTestTags)
        val supportVisible = SupportDestination.ROOT_TEST_TAG in tags

        assertTrue(
            "precondition: the outgoing activity's support window is still attached during overlap, " +
                "tags were $tags",
            supportVisible,
        )
        assertEquals(
            "the incoming activity's route is what labels the snapshot",
            "DashboardScreen",
            state.route,
        )
        // The gap, stated as an assertion so it is impossible to lose: the
        // retained screen is labelled with a NON-excluded route while a support
        // window is simultaneously in the captured tree.
        assertTrue(
            "documented gap: support content is in the captured tree under a non-excluded route",
            supportVisible && state.route !in setOf(SupportDestination.ROUTE, "ConversationOverlay"),
        )

        // ...and pinned again one layer down, where the harm actually lands.
        // `state.screen` is the IR `sessionCreateBody()` ships verbatim; the
        // tree assertion above would still pass if someone closed the gap in
        // `AppStateManager` while leaving the captured tree mixed, so this is
        // the assertion that has to fail when the gap is genuinely fixed.
        val ir = requireNotNull(state.screen) { "the gap needs a retained screen to show up in" }
        val labels = ir.components.map { it.label }
        assertEquals(
            "the PUBLISHED IR carries the incoming activity's non-excluded route",
            "DashboardScreen",
            ir.screen,
        )
        assertTrue(
            "documented gap: support-surface components reach the published IR, not just the raw " +
                "tree -- labels were $labels",
            SUPPORT_ONLY_LABEL in labels,
        )
    }

    private fun settle() {
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(600))
    }

    private fun rootCount(activity: MainActivity): Int = latestTree(activity).roots.size

    private fun latestTree(activity: MainActivity) = runBlocking {
        withTimeout(10_000L) { activity.uiTreeCollector.tree.first() }
    }

    private fun allTestTags(node: RawSemanticsNode): List<String> =
        listOfNotNull(node.testTag) + node.children.flatMap(::allTestTags)
}

/**
 * A label only the support surface emits, used as the marker that support
 * content reached the published IR.
 *
 * **Deliberately the chat fallback, not "Call Support".** The obvious choice
 * is fragile in a direction that would defeat the assertion silently: docs/03
 * §3.1 puts a floating `SupportButton` on operational screens, and
 * `DashboardDestination` already anticipates hosting one. The day that ships
 * with `label = "Call Support"`, the marker becomes satisfiable from the
 * INCOMING activity's own legitimately-captured dashboard window, and this
 * test would keep passing while proving nothing -- the exact defect class it
 * exists to catch. "Chat with us instead" is the text-chat fallback, is not
 * part of the floating-button design, and so has no reason to migrate onto an
 * operational screen.
 *
 * Duplicated rather than imported because `HelpScreen.kt` has it as a literal,
 * not a constant. If that literal changes, this assertion stops proving
 * anything and must be re-pointed at whatever replaces it.
 */
private const val SUPPORT_ONLY_LABEL = "Chat with us instead"
