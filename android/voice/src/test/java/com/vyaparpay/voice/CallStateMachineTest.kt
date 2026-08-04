package com.vyaparpay.voice

import com.vyaparpay.core.network.ApiError
import com.vyaparpay.core.network.ConnectBundleDto
import com.vyaparpay.core.network.IceServerDto
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The pure reducer (docs/03 §3.2): every transition of the
 * `Idle → Requesting → Signaling → Connecting → InCall → Reconnecting → Ended`
 * diagram, plus the convergence rule — unmatched events are no-ops, terminal
 * states absorb everything.
 */
class CallStateMachineTest {

    private val machine = CallStateMachine()

    private val bundle = ConnectBundleDto(
        sessionId = "a1f3c9",
        signalingUrl = "wss://voice.vyapar.local/v1/signal",
        signalingToken = "st_hV4nQ9pXzL2mR8cT0kWyB6uJ",
        iceServers = listOf(IceServerDto(urls = listOf("stun:turn.vyapar.local:3478"))),
        expires = "2026-07-24T14:19:22+05:30",
    )

    private fun driveTo(state: CallState) {
        machine.dispatch(CallEvent.SupportTapped)
        if (state == CallState.Requesting) return
        machine.dispatch(CallEvent.SessionMinted(bundle))
        if (state == CallState.Signaling) return
        machine.dispatch(CallEvent.AnswerApplied)
        if (state == CallState.Connecting) return
        machine.dispatch(CallEvent.PeerConnected)
        if (state == CallState.InCall) return
        machine.dispatch(CallEvent.TransportLost)
        check(state == CallState.Reconnecting) { "driveTo cannot reach $state" }
    }

    // ------------------------------------------------------------------
    // Happy path
    // ------------------------------------------------------------------

    @Test
    fun `the full happy path walks the docs 03 §3_2 diagram with its effects`() {
        assertEquals(CallState.Idle, machine.state.value)

        assertEquals(
            listOf(CallEffect.MintSession),
            machine.dispatch(CallEvent.SupportTapped),
        )
        assertEquals(CallState.Requesting, machine.state.value)

        assertEquals(
            listOf(CallEffect.OpenTransport(bundle)),
            machine.dispatch(CallEvent.SessionMinted(bundle)),
        )
        assertEquals(CallState.Signaling, machine.state.value)

        assertEquals(emptyList<CallEffect>(), machine.dispatch(CallEvent.AnswerApplied))
        assertEquals(CallState.Connecting, machine.state.value)

        // Media up, `ctx` open: the one edge that binds the capture pipeline
        // (docs/03 §3.10). Audio focus is NOT here — VoiceCallCoordinator
        // takes it off the *state* back at Requesting, before the mic opens.
        assertEquals(
            listOf(CallEffect.BindContextPublisher),
            machine.dispatch(CallEvent.PeerConnected),
        )
        assertEquals(CallState.InCall, machine.state.value)

        assertEquals(
            listOf(
                CallEffect.SendBye("user_hangup"),
                CallEffect.ReleaseCall,
                CallEffect.EndSessionOnApi,
            ),
            machine.dispatch(CallEvent.UserHungUp),
        )
        assertEquals(CallState.Ended(EndReason.USER_HUNG_UP), machine.state.value)
    }

    // ------------------------------------------------------------------
    // Failure branches, per phase
    // ------------------------------------------------------------------

    @Test
    fun `a failed session mint ends the attempt with nothing to tear down`() {
        driveTo(CallState.Requesting)

        val effects = machine.dispatch(CallEvent.Failed(ApiError.RATE_LIMITED))

        // docs/13 §2.1: a 429/503 leaves nothing on the voice-worker.
        assertEquals(emptyList<CallEffect>(), effects)
        assertEquals(CallState.Ended(EndReason.SETUP_FAILED), machine.state.value)
    }

    @Test
    fun `a signaling failure disposes the half-built peer`() {
        driveTo(CallState.Signaling)

        val effects = machine.dispatch(CallEvent.Failed(ApiError.AUTH_INVALID_TOKEN))

        assertEquals(listOf(CallEffect.ReleaseCall), effects)
        assertEquals(CallState.Ended(EndReason.SETUP_FAILED), machine.state.value)
    }

    @Test
    fun `the socket dying during signaling is a setup failure`() {
        driveTo(CallState.Signaling)

        val effects = machine.dispatch(CallEvent.SignalingClosed)

        assertEquals(listOf(CallEffect.ReleaseCall), effects)
        assertEquals(CallState.Ended(EndReason.SETUP_FAILED), machine.state.value)
    }

    @Test
    fun `media that never comes up is a setup failure`() {
        driveTo(CallState.Connecting)

        val effects = machine.dispatch(CallEvent.Failed(ApiError.UNKNOWN))

        assertEquals(listOf(CallEffect.ReleaseCall), effects)
        assertEquals(CallState.Ended(EndReason.SETUP_FAILED), machine.state.value)
    }

    @Test
    fun `transport lost while connecting ends the call - never a stuck live mic`() {
        // Review regression (HIGH): libwebrtc reports an initial-connect
        // ICE/DTLS failure as PeerConnectionState.FAILED, which arrives here
        // as TransportLost - the SAME event as a post-connect loss. In
        // Connecting there is no call to salvage: it must end and release,
        // not fall through to stay() holding a live mic forever.
        driveTo(CallState.Connecting)

        val effects = machine.dispatch(CallEvent.TransportLost)

        assertEquals(listOf(CallEffect.ReleaseCall), effects)
        assertEquals(CallState.Ended(EndReason.SETUP_FAILED), machine.state.value)
    }

    @Test
    fun `hanging up before the bundle arrives cancels the attempt`() {
        driveTo(CallState.Requesting)

        val effects = machine.dispatch(CallEvent.UserHungUp)

        assertEquals(listOf(CallEffect.ReleaseCall), effects)
        assertEquals(CallState.Ended(EndReason.USER_HUNG_UP), machine.state.value)
    }

    // ------------------------------------------------------------------
    // Mid-call: bye, transport loss, grace
    // ------------------------------------------------------------------

    @Test
    fun `a server bye mid-call ends the call without echoing a bye back`() {
        driveTo(CallState.InCall)

        val effects = machine.dispatch(CallEvent.RemoteBye("agent_hangup"))

        assertEquals(listOf(CallEffect.ReleaseCall), effects)
        assertEquals(CallState.Ended(EndReason.REMOTE_HUNG_UP), machine.state.value)
    }

    @Test
    fun `transport loss mid-call arms the 30s grace, resume disarms it`() {
        driveTo(CallState.InCall)

        assertEquals(
            listOf(CallEffect.StartReconnectGrace),
            machine.dispatch(CallEvent.TransportLost),
        )
        assertEquals(CallState.Reconnecting, machine.state.value)

        assertEquals(
            listOf(CallEffect.CancelReconnectGrace),
            machine.dispatch(CallEvent.TransportResumed),
        )
        assertEquals(CallState.InCall, machine.state.value)
    }

    @Test
    fun `the signaling socket dying mid-call is a bounded-grace case, not an instant end`() {
        driveTo(CallState.InCall)

        val effects = machine.dispatch(CallEvent.SignalingClosed)

        assertEquals(listOf(CallEffect.StartReconnectGrace), effects)
        assertEquals(CallState.Reconnecting, machine.state.value)
    }

    @Test
    fun `grace expiry tears down and lets the backend finalize`() {
        driveTo(CallState.Reconnecting)

        val effects = machine.dispatch(CallEvent.GraceExpired)

        assertEquals(listOf(CallEffect.ReleaseCall, CallEffect.EndSessionOnApi), effects)
        assertEquals(CallState.Ended(EndReason.GRACE_EXPIRED), machine.state.value)
    }

    @Test
    fun `the user can hang up while reconnecting`() {
        driveTo(CallState.Reconnecting)

        val effects = machine.dispatch(CallEvent.UserHungUp)

        assertEquals(
            listOf(
                CallEffect.SendBye("user_hangup"),
                CallEffect.ReleaseCall,
                CallEffect.EndSessionOnApi,
            ),
            effects,
        )
        assertEquals(CallState.Ended(EndReason.USER_HUNG_UP), machine.state.value)
    }

    // ------------------------------------------------------------------
    // Convergence: stray events are no-ops
    // ------------------------------------------------------------------

    @Test
    fun `terminal states absorb every event`() {
        driveTo(CallState.InCall)
        machine.dispatch(CallEvent.UserHungUp)
        val terminal = machine.state.value

        val strays = listOf(
            CallEvent.SupportTapped,
            CallEvent.SessionMinted(bundle),
            CallEvent.AnswerApplied,
            CallEvent.PeerConnected,
            CallEvent.TransportLost,
            CallEvent.TransportResumed,
            CallEvent.GraceExpired,
            CallEvent.Failed(ApiError.INTERNAL),
            CallEvent.UserHungUp,
            CallEvent.RemoteBye("error"),
            CallEvent.SignalingClosed,
        )
        strays.forEach { event ->
            assertTrue(machine.dispatch(event).isEmpty())
            assertEquals(terminal, machine.state.value)
        }
    }

    @Test
    fun `idle ignores everything except the support tap`() {
        assertTrue(machine.dispatch(CallEvent.PeerConnected).isEmpty())
        assertTrue(machine.dispatch(CallEvent.UserHungUp).isEmpty())
        assertTrue(machine.dispatch(CallEvent.GraceExpired).isEmpty())
        assertEquals(CallState.Idle, machine.state.value)
    }

    @Test
    fun `a mint completing after the user gave up cannot resurrect the attempt`() {
        driveTo(CallState.Requesting)
        machine.dispatch(CallEvent.UserHungUp)

        val effects = machine.dispatch(CallEvent.SessionMinted(bundle))

        assertTrue(effects.isEmpty())
        assertEquals(CallState.Ended(EndReason.USER_HUNG_UP), machine.state.value)
    }
}
