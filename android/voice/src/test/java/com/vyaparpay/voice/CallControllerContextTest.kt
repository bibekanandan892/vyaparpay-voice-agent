package com.vyaparpay.voice

import com.vyaparpay.core.network.ApiResult
import com.vyaparpay.core.network.ConnectBundleDto
import com.vyaparpay.core.network.IceServerDto
import com.vyaparpay.core.network.SessionCreateRequestDto
import com.vyaparpay.core.network.SessionEndDto
import com.vyaparpay.core.network.SessionSummaryDto
import com.vyaparpay.core.network.VyaparApi
import com.vyaparpay.core.screencontext.ContextChannel
import com.vyaparpay.voice.context.CallContextPublisher
import com.vyaparpay.voice.signaling.OutboundSignal
import com.vyaparpay.voice.signaling.SignalFrame
import com.vyaparpay.voice.signaling.SignalingClient
import com.vyaparpay.voice.signaling.SignalingTarget
import com.vyaparpay.voice.webrtc.RemoteIceCandidate
import com.vyaparpay.voice.webrtc.RtcEvent
import com.vyaparpay.voice.webrtc.Sdp
import com.vyaparpay.voice.webrtc.WebRtcClient
import com.vyaparpay.voice.webrtc.WebRtcException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.launchIn
import kotlinx.coroutines.flow.onEach
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The Phase-4 T8b wiring: `CallEffect.BindContextPublisher` attaching the
 * capture pipeline to the live `ctx` channel, the gap-recovery downlink, and
 * — the part this file exists for — **teardown**.
 *
 * A publisher collector that outlives its call keeps shipping the merchant's
 * screen contents into a dead channel, or into the next call's. That is a
 * privacy failure, not a leak, so "after the call ends, a further screen
 * emission produces no further send" is asserted directly rather than
 * inferred from a cancelled `Job`.
 *
 * [FakeCallContextPublisher] stands in for `ScreenContextPublisher`, which
 * cannot be constructed from this source set at all — see
 * [CallContextPublisher]'s own kdoc for that seam's reasoning. The fake
 * reproduces the one behaviour that matters here: `start` launches a
 * collector on the supplied scope, and cancelling that scope is the only way
 * to stop it (the real class has no `stop()` either).
 */
@OptIn(ExperimentalCoroutinesApi::class)
class CallControllerContextTest {

    private val api = FakeVyaparApi()
    private val signaling = FakeSignalingClient()
    private val webRtc = FakeContextWebRtcClient()
    private val publisher = FakeCallContextPublisher()

    private val request = SessionCreateRequestDto(
        userId = "usr_rajesh01",
        screenContext = null,
        recentEvents = emptyList(),
    )

    private fun TestScope.controller(): CallController = CallController(
        api = api,
        signaling = signaling,
        webRtc = webRtc,
        scope = backgroundScope,
        contextPublisher = publisher,
    )

    private fun TestScope.driveToInCall(controller: CallController) {
        controller.startCall(request)
        runCurrent()
        signaling.frames.tryEmit(SignalFrame.Answer("v=0-answer"))
        runCurrent()
        webRtc.events.tryEmit(RtcEvent.PeerConnected)
        runCurrent()
        check(controller.state.value == CallState.InCall) { "expected InCall, was ${controller.state.value}" }
    }

    // ------------------------------------------------------------------
    // Bind: only once media (and therefore the `ctx` channel) is up
    // ------------------------------------------------------------------

    @Test
    fun `nothing is published before the peer connects`() = runTest {
        val controller = controller()
        controller.startCall(request)
        runCurrent()

        // Signaling is up and the offer is out, but SCTP is not negotiated
        // until the answer lands — publishing here would be guaranteed loss.
        assertEquals(CallState.Signaling, controller.state.value)
        assertNull(publisher.boundChannel)

        publisher.screenState.value = "PaymentScreen"
        runCurrent()
        assertTrue(webRtc.sent.isEmpty())
    }

    @Test
    fun `PeerConnected binds the publisher to the live ctx channel`() = runTest {
        val controller = controller()
        driveToInCall(controller)

        assertEquals(1, publisher.startCallCount)
        assertNotNull("the publisher must be handed a channel", publisher.boundChannel)

        publisher.screenState.value = "PaymentScreen"
        runCurrent()

        // Proves the whole path, not just that start() was called: the bytes
        // the publisher wrote came out of WebRtcClient.sendContext.
        assertEquals(listOf("PaymentScreen"), webRtc.sentAsText())
    }

    @Test
    fun `a call with no capture pipeline still connects - context degrades, conversation does not`() = runTest {
        val contextFree = CallController(
            api = api,
            signaling = signaling,
            webRtc = webRtc,
            scope = backgroundScope,
        )

        driveToInCall(contextFree)

        assertEquals(CallState.InCall, contextFree.state.value)
        assertEquals(0, publisher.startCallCount)
    }

    @Test
    fun `a mid-call reconnect resumes without binding a second publisher`() = runTest {
        val controller = controller()
        driveToInCall(controller)

        webRtc.events.tryEmit(RtcEvent.TransportLost)
        runCurrent()
        assertEquals(CallState.Reconnecting, controller.state.value)
        webRtc.events.tryEmit(RtcEvent.TransportResumed)
        runCurrent()
        assertEquals(CallState.InCall, controller.state.value)

        // Two publishers racing one seq counter over one channel reads to the
        // server as a permanent gap storm. The binding must survive the
        // reconnect untouched, not be re-established.
        assertEquals(1, publisher.startCallCount)

        publisher.screenState.value = "DashboardScreen"
        runCurrent()
        assertEquals(listOf("DashboardScreen"), webRtc.sentAsText())
    }

    // ------------------------------------------------------------------
    // Teardown: the call ends, the capture stops. Every terminal path.
    // ------------------------------------------------------------------

    @Test
    fun `a user hang-up stops the publisher - a later screen change ships nothing`() = runTest {
        val controller = controller()
        driveToInCall(controller)

        publisher.screenState.value = "PaymentScreen"
        runCurrent()
        assertEquals(1, webRtc.sent.size)

        controller.hangUp()
        runCurrent()
        assertEquals(CallState.Ended(EndReason.USER_HUNG_UP), controller.state.value)

        // The merchant navigates on with the call over. Nothing may leave.
        publisher.screenState.value = "SettlementsScreen"
        runCurrent()
        publisher.screenState.value = "OrdersScreen"
        runCurrent()

        assertEquals(listOf("PaymentScreen"), webRtc.sentAsText())
    }

    @Test
    fun `a remote bye stops the publisher too`() = runTest {
        val controller = controller()
        driveToInCall(controller)

        signaling.frames.tryEmit(SignalFrame.Bye("agent_hangup"))
        runCurrent()
        assertEquals(CallState.Ended(EndReason.REMOTE_HUNG_UP), controller.state.value)

        publisher.screenState.value = "SettlementsScreen"
        runCurrent()

        assertTrue(webRtc.sentAsText().isEmpty())
    }

    @Test
    fun `the downlink pump dies with the call as well`() = runTest {
        val controller = controller()
        driveToInCall(controller)

        controller.hangUp()
        runCurrent()

        webRtc.incoming.tryEmit(REQUEST_SNAPSHOT_FRAME)
        runCurrent()

        assertEquals(0, publisher.requestFullSnapshotCount)
    }

    // ------------------------------------------------------------------
    // Gap recovery: the ctx.request_snapshot downlink
    // ------------------------------------------------------------------

    @Test
    fun `ctx request_snapshot on the downlink asks the publisher for a fresh snapshot`() = runTest {
        val controller = controller()
        driveToInCall(controller)

        webRtc.incoming.tryEmit(REQUEST_SNAPSHOT_FRAME)
        runCurrent()

        assertEquals(1, publisher.requestFullSnapshotCount)
    }

    @Test
    fun `every request_snapshot is answered, not just the first`() = runTest {
        val controller = controller()
        driveToInCall(controller)

        repeat(3) {
            webRtc.incoming.tryEmit(REQUEST_SNAPSHOT_FRAME)
            runCurrent()
        }

        assertEquals(3, publisher.requestFullSnapshotCount)
    }

    @Test
    fun `overlay frames on the shared channel are ignored, and the call stays up`() = runTest {
        val controller = controller()
        driveToInCall(controller)

        for (frame in OVERLAY_FRAMES) {
            webRtc.incoming.tryEmit(frame.toByteArray(Charsets.UTF_8))
            runCurrent()
        }

        assertEquals(0, publisher.requestFullSnapshotCount)
        assertEquals(CallState.InCall, controller.state.value)
    }

    @Test
    fun `a malformed downlink frame is ignored and does not end the call`() = runTest {
        val controller = controller()
        driveToInCall(controller)

        webRtc.incoming.tryEmit("}{ not json".toByteArray(Charsets.UTF_8))
        runCurrent()

        assertEquals(0, publisher.requestFullSnapshotCount)
        assertEquals(CallState.InCall, controller.state.value)

        // And the pump survived it — the next real request still lands.
        webRtc.incoming.tryEmit(REQUEST_SNAPSHOT_FRAME)
        runCurrent()
        assertEquals(1, publisher.requestFullSnapshotCount)
    }

    @Test
    fun `a publisher that throws while recovering does not end the call`() = runTest {
        publisher.failOnRequestFullSnapshot = true
        val controller = controller()
        driveToInCall(controller)

        webRtc.incoming.tryEmit(REQUEST_SNAPSHOT_FRAME)
        runCurrent()

        assertEquals(CallState.InCall, controller.state.value)

        // The pump is still alive for the next attempt (docs/08 §3.2: a lost
        // recovery costs one round trip of staleness, never the call).
        publisher.failOnRequestFullSnapshot = false
        webRtc.incoming.tryEmit(REQUEST_SNAPSHOT_FRAME)
        runCurrent()
        assertEquals(1, publisher.requestFullSnapshotCount)
    }

    @Test
    fun `a send failure mid-call is absorbed and the call keeps running`() = runTest {
        val controller = controller()
        driveToInCall(controller)

        // The routine case: the channel closed under us (reconnect grace).
        webRtc.failSends = true
        publisher.screenState.value = "PaymentScreen"
        runCurrent()

        assertEquals(CallState.InCall, controller.state.value)

        webRtc.failSends = false
        publisher.screenState.value = "DashboardScreen"
        runCurrent()
        assertEquals(listOf("DashboardScreen"), webRtc.sentAsText())
    }

    /**
     * Review fix (MEDIUM): [WebRtcContextChannel.droppedFrameCount] is
     * documented as "the observability that keeps this from being a silent
     * failure", but the adapter used to be constructed inline and discarded,
     * so in production nothing could ever read it — the counter was live only
     * inside its own unit test. This asserts the count is reachable from the
     * controller, which is what makes that documented intent true.
     */
    @Test
    fun `frames dropped while the channel is down are counted and reachable from the controller`() = runTest {
        val controller = controller()
        assertEquals(0L, controller.contextFramesDropped)

        driveToInCall(controller)
        assertEquals(0L, controller.contextFramesDropped)

        webRtc.failSends = true
        publisher.screenState.value = "PaymentScreen"
        runCurrent()

        assertEquals(1L, controller.contextFramesDropped)

        // Survives teardown: the count is most interesting after the call.
        controller.hangUp()
        runCurrent()
        assertEquals(1L, controller.contextFramesDropped)
    }

    private companion object {
        private val REQUEST_SNAPSHOT_FRAME =
            """{"v":1,"type":"ctx.request_snapshot","seq":7,"ts":1784536455900,"payload":{"last_good_seq":44}}"""
                .toByteArray(Charsets.UTF_8)

        private val OVERLAY_FRAMES = listOf(
            """{"v":1,"type":"transcript.partial","seq":11,"ts":1,"payload":{"turn":2,"role":"user","text":"haan"}}""",
            """{"v":1,"type":"transcript.final","seq":9,"ts":1,"payload":{"turn":1,"role":"agent","text":"Hi"}}""",
            """{"v":1,"type":"agent.state","seq":12,"ts":1,"payload":{"state":"thinking"}}""",
        )
    }
}

// ------------------------------------------------------------------
// Fakes
// ------------------------------------------------------------------

/**
 * Reproduces the one `ScreenContextPublisher` behaviour this wiring depends
 * on: [start] launches a collector on the caller's scope, and there is no
 * `stop()` — cancelling that scope is the only teardown. Each screen-state
 * value is written to the channel verbatim so a test can assert on the exact
 * bytes that reached the wire.
 */
private class FakeCallContextPublisher : CallContextPublisher {

    val screenState = MutableStateFlow<String?>(null)
    var startCallCount: Int = 0
        private set
    var requestFullSnapshotCount: Int = 0
        private set
    var boundChannel: ContextChannel? = null
        private set
    var failOnRequestFullSnapshot: Boolean = false

    override fun start(channel: ContextChannel, scope: CoroutineScope) {
        startCallCount++
        boundChannel = channel
        screenState
            .onEach { screen -> screen?.let { channel.send(it.toByteArray(Charsets.UTF_8)) } }
            .launchIn(scope)
    }

    override suspend fun requestFullSnapshot() {
        if (failOnRequestFullSnapshot) throw IllegalStateException("recovery snapshot failed")
        requestFullSnapshotCount++
    }
}

private class FakeContextWebRtcClient : WebRtcClient {

    override val events = MutableSharedFlow<RtcEvent>(replay = 16, extraBufferCapacity = 16)
    override val incoming = MutableSharedFlow<ByteArray>(extraBufferCapacity = 16)

    val sent: MutableList<ByteArray> = mutableListOf()
    var failSends: Boolean = false

    fun sentAsText(): List<String> = sent.map { String(it, Charsets.UTF_8) }

    override suspend fun start(iceServers: List<IceServerDto>): Sdp = Sdp("v=0-fake-offer")
    override suspend fun applyAnswer(sdp: Sdp) = Unit
    override fun addRemoteCandidate(candidate: RemoteIceCandidate) = Unit
    override suspend fun restartIce(): Sdp = Sdp("v=0-fake-restart")
    override fun setMuted(muted: Boolean) = Unit

    override suspend fun sendContext(bytes: ByteArray) {
        if (failSends) throw WebRtcException("'ctx' data channel is not open")
        sent += bytes
    }

    override suspend fun close() = Unit
}

private class FakeSignalingClient : SignalingClient {

    override val frames = MutableSharedFlow<SignalFrame>(replay = 8, extraBufferCapacity = 8)
    val sent: MutableList<OutboundSignal> = mutableListOf()

    override suspend fun connect(target: SignalingTarget) = Unit

    override fun send(frame: OutboundSignal) {
        sent += frame
    }

    override fun close() = Unit
}

private class FakeVyaparApi : VyaparApi {

    val bundle = ConnectBundleDto(
        sessionId = "a1f3c9",
        signalingUrl = "wss://voice.vyapar.local/v1/signal",
        signalingToken = "st_hV4nQ9pXzL2mR8cT0kWyB6uJ",
        iceServers = listOf(IceServerDto(urls = listOf("stun:turn.vyapar.local:3478"))),
        expires = "2026-07-24T14:19:22+05:30",
    )

    override suspend fun createSession(body: SessionCreateRequestDto): ApiResult<ConnectBundleDto> =
        ApiResult.Success(bundle)

    override suspend fun remintSessionToken(sessionId: String): ApiResult<ConnectBundleDto> =
        throw NotImplementedError("unused in these tests")

    override suspend fun endSession(sessionId: String): ApiResult<SessionEndDto> =
        ApiResult.Success(SessionEndDto(sessionId, "ended"))

    override suspend fun getSessionSummary(sessionId: String): ApiResult<SessionSummaryDto> =
        throw NotImplementedError("unused in these tests")
}
