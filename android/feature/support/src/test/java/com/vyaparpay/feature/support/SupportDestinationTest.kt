package com.vyaparpay.feature.support

import com.vyaparpay.core.ui.testtag.TestTagRole
import com.vyaparpay.core.ui.testtag.TestTagRoles
import com.vyaparpay.voice.CallState
import com.vyaparpay.voice.holdsLiveCall
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [SupportDestination.IS_CAPTURED] == `false` is this module's *entire*
 * capture-exclusion contract -- there is no other exclusion code anywhere in
 * `:feature:support` (see [HelpScreen]'s kdoc and `HelpViewModel`'s kdoc, and
 * `NavigationTracker`'s own comment on the `HelpScreen -> "support"`
 * route-to-flow row: "that exclusion is ScreenContextPublisher's job, out of
 * scope here"). Enforcing that flag -- refusing to overwrite the last
 * operational snapshot while the user is on this route -- is a future
 * `ScreenContextPublisher`/`AppStateManager` job, out of this module's scope.
 */
class SupportDestinationTest {

    @Test
    fun `the support hub is excluded from capture`() {
        // docs/07 §2.1: the agent gets the screen the merchant was stuck on, not
        // the screen they went to for help.
        assertFalse(SupportDestination.IS_CAPTURED)
    }

    @Test
    fun `the support hub reports the support flow`() {
        assertEquals("support", SupportDestination.FLOW)
    }

    @Test
    fun `the support feature can see the call vocabulary it is allowed to depend on`() {
        // Compile-time proof of the one permitted feature-to-voice edge; if the
        // dependency were dropped this test would not compile.
        assertTrue(CallState.InCall.holdsLiveCall)
    }

    @Test
    fun `the root tag is a screen container, not a mis-typed interactive role`() {
        // A screen root that accidentally matched an interactive convention
        // would show up in the IR as a primary CTA -- not that it matters for
        // capture here (this screen is excluded), but the convention stays
        // clean regardless, per this class's own kdoc.
        assertEquals(
            TestTagRole.UNRESOLVED,
            TestTagRoles.roleFor(SupportDestination.ROOT_TEST_TAG),
        )
    }
}
