package com.vyaparpay

import com.vyaparpay.navigation.AppRoute
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [shouldShowReturnToCallChip] only — the visibility decision, which is the
 * actual bug-prone part of this feature. A plain function on purpose (see
 * its own kdoc) so it needs no Compose/Robolectric infrastructure here.
 *
 * **[ReturnToCallChip] itself is not independently tested here.** `:app`'s
 * own production manifest declares only `.MainActivity` (see [MainActivity]'s
 * kdoc, and its own note that a bare `ComponentActivity` was tried and
 * confirmed unresolvable to Robolectric's `ActivityScenario` inside this
 * module) — that holds even with a test-only `src/test/AndroidManifest.xml`
 * declaring `androidx.activity.ComponentActivity`, confirmed empirically
 * again while building this feature (the merge for an application module
 * behaves differently than the library-module pattern
 * `com.vyaparpay.feature.support.CallScreenTest` relies on one module over).
 *
 * That gap is real, not papered over: the composable's specific shape —
 * `Icon(contentDescription = null)` and a visible `Text` label as merged
 * siblings *inside* the same `Modifier.trackedClickable` content — is not
 * the same shape any *other* passing test in this codebase happens to cover
 * either (`SupportButton` has no icon; `CallControlButton` deliberately
 * moves its label *outside* the clickable and clears its semantics instead).
 * `com.vyaparpay.feature.support.MergedIconTextClickableSemanticsTest`
 * closes that specific gap with a real, passing Compose test reproducing the
 * exact structural shape — it lives in `:feature:support` because that is
 * where the working Robolectric infrastructure is, not because the shape
 * itself belongs to that module. What remains genuinely untested is only
 * `ReturnToCallChip`'s own wiring (icon choice, colors, padding) — visual
 * detail, not the accessibility/tap-target correctness that actually matters
 * and that test now verifies.
 */
class ReturnToCallChipTest {

    @Test
    fun `hidden when no call is live, regardless of route`() {
        assertFalse(shouldShowReturnToCallChip(isCallLive = false, currentRoute = AppRoute.DASHBOARD.route))
        assertFalse(shouldShowReturnToCallChip(isCallLive = false, currentRoute = AppRoute.SUPPORT.route))
        assertFalse(shouldShowReturnToCallChip(isCallLive = false, currentRoute = null))
    }

    @Test
    fun `shown when a call is live and the merchant is elsewhere`() {
        assertTrue(shouldShowReturnToCallChip(isCallLive = true, currentRoute = AppRoute.DASHBOARD.route))
        assertTrue(shouldShowReturnToCallChip(isCallLive = true, currentRoute = AppRoute.PAYMENT.route))
        assertTrue(shouldShowReturnToCallChip(isCallLive = true, currentRoute = null))
    }

    @Test
    fun `hidden when a call is live but the merchant is already on the support route`() {
        // CallScreen already renders the call there -- a chip back to the
        // same screen would be redundant.
        assertFalse(shouldShowReturnToCallChip(isCallLive = true, currentRoute = AppRoute.SUPPORT.route))
    }
}
