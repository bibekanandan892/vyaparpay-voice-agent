package com.vyaparpay.voice

import com.vyaparpay.core.network.ApiError
import com.vyaparpay.core.network.ApiResult
import com.vyaparpay.core.network.ConnectBundleDto
import com.vyaparpay.core.network.IceServerDto
import com.vyaparpay.core.network.SessionCreateRequestDto
import com.vyaparpay.core.network.SessionEndDto
import com.vyaparpay.core.network.SessionSummaryDto
import com.vyaparpay.core.network.VyaparApi
import com.vyaparpay.voice.signaling.OutboundSignal
import com.vyaparpay.voice.signaling.SignalFrame
import com.vyaparpay.voice.signaling.SignalingClient
import com.vyaparpay.voice.signaling.SignalingClosedReason
import com.vyaparpay.voice.signaling.SignalingConnectException
import com.vyaparpay.voice.signaling.SignalingTarget
import com.vyaparpay.voice.webrtc.LocalIceCandidate
import com.vyaparpay.voice.webrtc.RemoteIceCandidate
import com.vyaparpay.voice.webrtc.RtcEvent
import com.vyaparpay.voice.webrtc.Sdp
import com.vyaparpay.voice.webrtc.WebRtcClient
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [CallController] against fakes behind the [SignalingClient]/[WebRtcClient]
 * interfaces — the whole point of the seam: the full call sequence and every
 * failure branch run on a JVM with virtual time, no sockets, no libwebrtc.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class CallControllerTest {

    private val api = FakeVyaparApi()
    private val signaling = FakeSignalingClient()
    private val webRtc = FakeWebRtcClient()

    private val request = SessionCreateRequestDto(
        userId = "usr_rajesh01",
        screenContext = null,
        recentEvents = emptyList(),
    )

    private val bundle = ConnectBundleDto(
        sessionId = "a1f3c9",
        signalingUrl = "wss://voice.vyapar.local/v1/signal",
        signalingToken = "st_hV4nQ9pXzL2mR8cT0kWyB6uJ",
        iceServers = listOf(
            IceServerDto(urls = listOf("stun:turn.vyapar.local:3478")),
            IceServerDto(
                urls = listOf("turn:turn.vyapar.local:3478?transport=udp"),
                username = "1784537062:a1f3c9",
                credential = "kWx0mB4vQ2nT8hZJc6yUq1RfLpE=",
            ),
        ),
        expires = "2026-07-24T14:19:22+05:30",
    )

    private fun TestScope.controller(): CallController = CallController(
        api = api,
        signaling = signaling,
        webRtc = webRtc,
        scope = backgroundScope,
    )

    private suspend fun TestScope.driveToInCall(controller: CallController) {
        controller.startCall(request)
        runCurrent()
        signaling.frames.emit(SignalFrame.Answer("v=0-answer"))
        runCurrent()
        webRtc.events.emit(RtcEvent.PeerConnected)
        runCurrent()
        check(controller.state.value == CallState.InCall)
    }

    // ------------------------------------------------------------------
    // Happy path
    // ------------------------------------------------------------------

    @Test
    fun `the happy path mints, connects, offers, answers, connects media, and hangs up cleanly`() = runTest {
        val controller = controller()

        controller.startCall(request)
        runCurrent()

        // Bundle minted and both planes opened: WS with the bundle's
        // coordinates, peer started with its ice_servers verbatim, offer out.
        assertEquals(CallState.Signaling, controller.state.value)
        assertEquals(
            SignalingTarget(bundle.signalingUrl, bundle.sessionId, bundle.signalingToken),
            signaling.connectedTarget,
        )
        assertEquals(bundle.iceServers, webRtc.startedWith)
        assertEquals(OutboundSignal.Offer("v=0-fake-offer"), signaling.sent.first())

        signaling.frames.emit(SignalFrame.Answer("v=0-answer"))
        runCurrent()
        assertEquals(CallState.Connecting, controller.state.value)
        assertEquals(Sdp("v=0-answer"), webRtc.appliedAnswer)

        webRtc.events.emit(RtcEvent.PeerConnected)
        runCurrent()
        assertEquals(CallState.InCall, controller.state.value)

        // The armed answer timeout must be dead — a long call cannot trip it.
        advanceTimeBy(CallController.ANSWER_TIMEOUT_MILLIS * 10)
        runCurrent()
        assertEquals(CallState.InCall, controller.state.value)

        controller.hangUp()
        runCurrent()
        assertEquals(CallState.Ended(EndReason.USER_HUNG_UP), controller.state.value)
        assertEquals(OutboundSignal.Bye("user_hangup"), signaling.sent.last())
        assertEquals(listOf("a1f3c9"), api.endedSessions)
        assertEquals(1, webRtc.closeCount)
        assertEquals(1, signaling.closeCount)
    }

    @Test
    fun `local candidates trickle out as ice frames the moment they are found`() = runTest {
        val controller = controller()
        controller.startCall(request)
        runCurrent()

        webRtc.events.emit(
            RtcEvent.IceCandidateFound(
                LocalIceCandidate("candidate:1 1 udp 1 10.0.0.2 50000 typ host", "0", 0),
            ),
        )
        runCurrent()

        assertTrue(
            signaling.sent.contains(
                OutboundSignal.LocalIce("candidate:1 1 udp 1 10.0.0.2 50000 typ host", "0", 0),
            ),
        )
    }

    @Test
    fun `remote candidates apply to the peer, and the embedded-only shape still connects`() = runTest {
        val controller = controller()
        controller.startCall(request)
        runCurrent()

        // The demo server embeds every candidate in the answer SDP and sends
        // only the null end-of-candidates marker (peer_session.py judgment
        // call 3) — no trickled candidates at all must still reach InCall.
        signaling.frames.emit(SignalFrame.Answer("v=0-answer-with-embedded-candidates"))
        signaling.frames.emit(SignalFrame.RemoteIce(null, null, null))
        runCurrent()

        assertEquals(
            listOf(RemoteIceCandidate(null, null, null)),
            webRtc.remoteCandidates,
        )

        webRtc.events.emit(RtcEvent.PeerConnected)
        runCurrent()
        assertEquals(CallState.InCall, controller.state.value)

        // A genuinely trickled candidate is applied too.
        signaling.frames.emit(SignalFrame.RemoteIce("candidate:7 1 udp 7 1.2.3.4 999 typ srflx", "0", 0))
        runCurrent()
        assertEquals(
            RemoteIceCandidate("candidate:7 1 udp 7 1.2.3.4 999 typ srflx", "0", 0),
            webRtc.remoteCandidates.last(),
        )
    }

    // ------------------------------------------------------------------
    // Failure branches
    // ------------------------------------------------------------------

    @Test
    fun `a failed session create ends the call without touching either plane`() = runTest {
        api.createResult = ApiResult.Failure(ApiError.RATE_LIMITED, "slow down")
        val controller = controller()

        controller.startCall(request)
        runCurrent()

        assertEquals(CallState.Ended(EndReason.SETUP_FAILED), controller.state.value)
        assertNull(signaling.connectedTarget)
        assertNull(webRtc.startedWith)
        assertTrue(api.endedSessions.isEmpty())
    }

    @Test
    fun `a refused signaling connect ends the attempt`() = runTest {
        signaling.connectException = SignalingConnectException("refused")
        val controller = controller()

        controller.startCall(request)
        runCurrent()

        assertEquals(CallState.Ended(EndReason.SETUP_FAILED), controller.state.value)
        assertEquals(1, webRtc.closeCount) // the half-built peer is disposed
    }

    @Test
    fun `a server error frame during signaling is fatal for the setup`() = runTest {
        val controller = controller()
        controller.startCall(request)
        runCurrent()

        signaling.frames.emit(SignalFrame.ServerError("AUTH_INVALID_TOKEN", "invalid"))
        runCurrent()

        assertEquals(CallState.Ended(EndReason.SETUP_FAILED), controller.state.value)
        assertEquals(1, webRtc.closeCount)
        assertEquals(1, signaling.closeCount)
    }

    @Test
    fun `an answer that never comes times out into a setup failure`() = runTest {
        val controller = controller()
        controller.startCall(request)
        runCurrent()
        assertEquals(CallState.Signaling, controller.state.value)

        advanceTimeBy(CallController.ANSWER_TIMEOUT_MILLIS + 1)
        runCurrent()

        assertEquals(CallState.Ended(EndReason.SETUP_FAILED), controller.state.value)
    }

    @Test
    fun `signaling dropping mid-call bounds the call by the 30s grace`() = runTest {
        val controller = controller()
        driveToInCall(controller)

        signaling.frames.emit(SignalFrame.Closed(SignalingClosedReason.PING_TIMEOUT))
        runCurrent()
        assertEquals(CallState.Reconnecting, controller.state.value)

        advanceTimeBy(CallController.RECONNECT_GRACE_MILLIS + 1)
        runCurrent()

        assertEquals(CallState.Ended(EndReason.GRACE_EXPIRED), controller.state.value)
        assertEquals(1, webRtc.closeCount)
        assertEquals(listOf("a1f3c9"), api.endedSessions)
    }

    @Test
    fun `a transport blip that recovers inside the grace resumes the call`() = runTest {
        val controller = controller()
        driveToInCall(controller)

        webRtc.events.emit(RtcEvent.TransportLost)
        runCurrent()
        assertEquals(CallState.Reconnecting, controller.state.value)

        webRtc.events.emit(RtcEvent.TransportResumed)
        runCurrent()
        assertEquals(CallState.InCall, controller.state.value)

        // The grace timer must be disarmed — outliving it must not end the call.
        advanceTimeBy(CallController.RECONNECT_GRACE_MILLIS * 2)
        runCurrent()
        assertEquals(CallState.InCall, controller.state.value)
    }

    @Test
    fun `a server bye mid-call ends the call without echoing a bye`() = runTest {
        val controller = controller()
        driveToInCall(controller)

        signaling.frames.emit(SignalFrame.Bye("agent_hangup"))
        runCurrent()

        assertEquals(CallState.Ended(EndReason.REMOTE_HUNG_UP), controller.state.value)
        assertEquals(1, webRtc.closeCount)
        assertEquals(1, signaling.closeCount)
        assertTrue(signaling.sent.none { it is OutboundSignal.Bye })
        assertTrue(api.endedSessions.isEmpty())
    }

    @Test
    fun `mute is a straight passthrough to the media plane`() = runTest {
        val controller = controller()

        controller.setMuted(true)

        assertEquals(true, webRtc.lastMuted)
    }

    // ------------------------------------------------------------------
    // Fakes
    // ------------------------------------------------------------------

    private class FakeVyaparApi : VyaparApi {
        var createResult: ApiResult<ConnectBundleDto>? = null
        val endedSessions = mutableListOf<String>()
        lateinit var defaultBundle: ConnectBundleDto

        override suspend fun createSession(
            body: SessionCreateRequestDto,
        ): ApiResult<ConnectBundleDto> = createResult ?: ApiResult.Success(defaultBundle)

        override suspend fun remintSessionToken(
            sessionId: String,
        ): ApiResult<ConnectBundleDto> = throw NotImplementedError("unused in these tests")

        override suspend fun endSession(sessionId: String): ApiResult<SessionEndDto> {
            endedSessions += sessionId
            return ApiResult.Success(SessionEndDto(sessionId, "ended"))
        }

        override suspend fun getSessionSummary(
            sessionId: String,
        ): ApiResult<SessionSummaryDto> = throw NotImplementedError("unused in these tests")
    }

    private class FakeSignalingClient : SignalingClient {
        override val frames = MutableSharedFlow<SignalFrame>(replay = 16, extraBufferCapacity = 16)
        var connectedTarget: SignalingTarget? = null
        var connectException: Exception? = null
        val sent = mutableListOf<OutboundSignal>()
        var closeCount = 0

        override suspend fun connect(target: SignalingTarget) {
            connectException?.let { throw it }
            connectedTarget = target
        }

        override fun send(frame: OutboundSignal) {
            sent += frame
        }

        override fun close() {
            closeCount++
        }
    }

    private class FakeWebRtcClient : WebRtcClient {
        override val events = MutableSharedFlow<RtcEvent>(replay = 16, extraBufferCapacity = 16)
        override val incoming: SharedFlow<ByteArray> = MutableSharedFlow()
        var startedWith: List<IceServerDto>? = null
        var appliedAnswer: Sdp? = null
        val remoteCandidates = mutableListOf<RemoteIceCandidate>()
        var lastMuted: Boolean? = null
        var closeCount = 0

        override suspend fun start(iceServers: List<IceServerDto>): Sdp {
            startedWith = iceServers
            return Sdp("v=0-fake-offer")
        }

        override suspend fun applyAnswer(sdp: Sdp) {
            appliedAnswer = sdp
        }

        override fun addRemoteCandidate(candidate: RemoteIceCandidate) {
            remoteCandidates += candidate
        }

        override suspend fun restartIce(): Sdp = Sdp("v=0-fake-restart-offer")

        override fun setMuted(muted: Boolean) {
            lastMuted = muted
        }

        override suspend fun sendContext(bytes: ByteArray) = Unit

        override suspend fun close() {
            closeCount++
        }
    }

    init {
        api.defaultBundle = bundle
    }
}
