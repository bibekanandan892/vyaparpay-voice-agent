package com.vyaparpay.feature.support

import com.vyaparpay.voice.CallState
import com.vyaparpay.voice.EndReason
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The [CallState] -> [CallPhase] collapse, and the two derived flags the
 * status panel branches on.
 *
 * Worth its own file because the mapping is where `:voice`'s seven-state
 * vocabulary meets a UI that renders four — a wrong bucket here is invisible
 * in `CallViewModelTest` (which asserts the phases it expects) but shows the
 * merchant "Connected" over a call that is still dialling.
 */
class CallUiStateTest {

    @Test
    fun `every setup state collapses to CONNECTING`() {
        // :voice keeps these distinct because they fail differently
        // (CallState's own kdoc); to a merchant they are one spinner.
        assertEquals(CallPhase.CONNECTING, CallState.Requesting.toPhase())
        assertEquals(CallPhase.CONNECTING, CallState.Signaling.toPhase())
        assertEquals(CallPhase.CONNECTING, CallState.Connecting.toPhase())
    }

    @Test
    fun `live, reconnecting, idle and terminal states map one to one`() {
        assertEquals(CallPhase.IDLE, CallState.Idle.toPhase())
        assertEquals(CallPhase.IN_CALL, CallState.InCall.toPhase())
        assertEquals(CallPhase.RECONNECTING, CallState.Reconnecting.toPhase())
        assertEquals(CallPhase.ENDED, CallState.Ended(EndReason.USER_HUNG_UP).toPhase())
    }

    @Test
    fun `every end reason still maps to ENDED`() {
        EndReason.entries.forEach { reason ->
            assertEquals(
                "EndReason.$reason must be terminal",
                CallPhase.ENDED,
                CallState.Ended(reason).toPhase(),
            )
        }
    }

    @Test
    fun `an idle state with no notice draws nothing`() {
        assertFalse(CallUiState().isVisible)
    }

    @Test
    fun `a permission notice is visible even with no call`() {
        // The denial branches never start a call, so IDLE + notice is exactly
        // the state that must still render something.
        assertTrue(CallUiState(notice = CallNotice.MIC_BLOCKED).isVisible)
    }

    @Test
    fun `hang up is offered only while a call could still be running`() {
        assertTrue(CallUiState(phase = CallPhase.CONNECTING).canHangUp)
        assertTrue(CallUiState(phase = CallPhase.IN_CALL).canHangUp)
        assertTrue(CallUiState(phase = CallPhase.RECONNECTING).canHangUp)

        assertFalse(CallUiState(phase = CallPhase.IDLE).canHangUp)
        assertFalse(CallUiState(phase = CallPhase.ENDED).canHangUp)
    }
}
