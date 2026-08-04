package com.vyaparpay.voice.webrtc

import com.vyaparpay.core.network.IceServerDto
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [WebRtcContextChannel] against a fake [WebRtcClient] — the byte path and,
 * more importantly, the failure policy: shipping context must never be able
 * to take down a live call (docs/08 §7's "degrade context, never
 * conversation").
 */
class WebRtcContextChannelTest {

    private val webRtc = FakeWebRtcClient()
    private val channel = WebRtcContextChannel(webRtc)

    @Test
    fun `frames are handed to sendContext byte-for-byte`() = runTest {
        val frame = """{"v":1,"type":"ctx.snapshot","seq":1}""".toByteArray(Charsets.UTF_8)

        channel.send(frame)

        assertEquals(1, webRtc.sent.size)
        assertArrayEquals(frame, webRtc.sent.single())
        assertEquals(0L, channel.droppedFrameCount)
    }

    @Test
    fun `a closed data channel drops the frame instead of failing the call`() = runTest {
        // The exact throw PeerConnectionWebRtcClient.sendContext raises when
        // the channel is not OPEN — routine during the docs/06 §6 reconnect
        // grace, and between PeerConnected and SCTP actually opening.
        webRtc.failWith = WebRtcException("'ctx' data channel is not open")

        channel.send("{}".toByteArray(Charsets.UTF_8))

        assertEquals(1L, channel.droppedFrameCount)
    }

    @Test
    fun `an unexpected transport failure is dropped too, not propagated`() = runTest {
        // Native DataChannel.send on a disposed channel does not throw
        // WebRtcException; the policy is about the outcome, not the type.
        webRtc.failWith = IllegalStateException("native channel disposed")

        channel.send("{}".toByteArray(Charsets.UTF_8))

        assertEquals(1L, channel.droppedFrameCount)
    }

    @Test
    fun `drops accumulate so a persistently closed channel is observable`() = runTest {
        webRtc.failWith = WebRtcException("'ctx' data channel is not open")

        repeat(3) { channel.send("{}".toByteArray(Charsets.UTF_8)) }
        webRtc.failWith = null
        channel.send("{}".toByteArray(Charsets.UTF_8))

        assertEquals(3L, channel.droppedFrameCount)
        assertEquals(1, webRtc.sent.size)
    }

    @Test
    fun `cancellation is re-thrown - a torn-down call must not keep a collector alive`() = runTest {
        // Swallowing this would leave ScreenContextPublisher's collector
        // running past the end of the call, still shipping the merchant's
        // screen. That is the failure mode the call-scoped binding exists to
        // prevent, so it must NOT be treated as a droppable frame.
        webRtc.failWith = CancellationException("call torn down")

        var thrown = false
        try {
            channel.send("{}".toByteArray(Charsets.UTF_8))
        } catch (_: CancellationException) {
            thrown = true
        }

        assertTrue("CancellationException must propagate", thrown)
        assertEquals(0L, channel.droppedFrameCount)
    }
}

private class FakeWebRtcClient : WebRtcClient {

    val sent: MutableList<ByteArray> = mutableListOf()
    var failWith: Throwable? = null

    override val events: SharedFlow<RtcEvent> = MutableSharedFlow()
    override val incoming: SharedFlow<ByteArray> = MutableSharedFlow()

    override suspend fun sendContext(bytes: ByteArray) {
        failWith?.let { throw it }
        sent += bytes
    }

    override suspend fun start(iceServers: List<IceServerDto>): Sdp = Sdp("v=0-fake-offer")
    override suspend fun applyAnswer(sdp: Sdp) = Unit
    override fun addRemoteCandidate(candidate: RemoteIceCandidate) = Unit
    override suspend fun restartIce(): Sdp = Sdp("v=0-fake-restart")
    override fun setMuted(muted: Boolean) = Unit
    override suspend fun close() = Unit
}
