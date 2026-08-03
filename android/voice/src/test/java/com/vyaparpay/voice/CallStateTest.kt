package com.vyaparpay.voice

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class CallStateTest {

    @Test
    fun `only InCall and Reconnecting claim a live peer connection`() {
        assertTrue(CallState.InCall.holdsLiveCall)
        assertTrue(CallState.Reconnecting.holdsLiveCall)

        assertFalse(CallState.Idle.holdsLiveCall)
        assertFalse(CallState.Requesting.holdsLiveCall)
        assertFalse(CallState.Signaling.holdsLiveCall)
        assertFalse(CallState.Connecting.holdsLiveCall)
    }

    @Test
    fun `an ended call never claims a live peer connection, whatever ended it`() {
        EndReason.entries.forEach { reason ->
            assertFalse(reason.name, CallState.Ended(reason).holdsLiveCall)
        }
    }

    @Test
    fun `the foreground service spans the whole attempt, not just the connected part`() {
        // The FGS starts on Idle -> Requesting, well before media is up, because
        // the mic-type FGS must be started from a guaranteed-foreground moment
        // (docs/03 §3.3) — not once the call happens to succeed.
        assertTrue(CallState.Requesting.requiresForegroundService)
        assertTrue(CallState.Signaling.requiresForegroundService)
        assertTrue(CallState.Connecting.requiresForegroundService)
        assertTrue(CallState.InCall.requiresForegroundService)
        assertTrue(CallState.Reconnecting.requiresForegroundService)

        assertFalse(CallState.Idle.requiresForegroundService)
        assertFalse(CallState.Ended(EndReason.USER_HUNG_UP).requiresForegroundService)
    }
}
