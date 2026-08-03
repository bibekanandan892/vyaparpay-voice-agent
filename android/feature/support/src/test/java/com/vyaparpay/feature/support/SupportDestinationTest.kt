package com.vyaparpay.feature.support

import com.vyaparpay.voice.CallState
import com.vyaparpay.voice.holdsLiveCall
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

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
}
