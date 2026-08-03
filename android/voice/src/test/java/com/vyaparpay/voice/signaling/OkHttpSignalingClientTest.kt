package com.vyaparpay.voice.signaling

import app.cash.turbine.test
import java.util.concurrent.CountDownLatch
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.runBlocking
import okhttp3.OkHttpClient
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * [OkHttpSignalingClient] against a real local WebSocket server —
 * MockWebServer's upgrade support gives us an actual RFC 6455 handshake, so
 * the URL auth parameters, the envelope routing, and the keepalive loop are
 * exercised over a live socket, not a mock of one.
 */
class OkHttpSignalingClientTest {

    private lateinit var server: MockWebServer
    private lateinit var okHttp: OkHttpClient
    private lateinit var scope: CoroutineScope

    private val target: SignalingTarget
        get() = SignalingTarget(
            signalingUrl = server.url(SIGNAL_PATH).toString(),
            sessionId = "a1f3c9",
            token = SECRET_TOKEN,
        )

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
        okHttp = OkHttpClient()
        scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    }

    @After
    fun tearDown() {
        scope.cancel()
        server.shutdown()
        okHttp.dispatcher.executorService.shutdown()
    }

    private fun client(pingIntervalMillis: Long = 10_000L): OkHttpSignalingClient =
        OkHttpSignalingClient(okHttp, scope, pingIntervalMillis)

    private fun upgrade(listener: ServerSide = ServerSide()): ServerSide {
        server.enqueue(MockResponse().withWebSocketUpgrade(listener))
        return listener
    }

    // ------------------------------------------------------------------
    // Connect: URL shape and refusal
    // ------------------------------------------------------------------

    @Test
    fun `connect carries session_id and token as query parameters on the signal path`() {
        upgrade()

        runBlocking { client().connect(target) }

        val request = server.takeRequest()
        val url = requireNotNull(request.requestUrl)
        assertEquals(SIGNAL_PATH, url.encodedPath)
        assertEquals("a1f3c9", url.queryParameter("session_id"))
        assertEquals(SECRET_TOKEN, url.queryParameter("token"))
    }

    @Test
    fun `connect converts a ws scheme to OkHttp's http URL model`() {
        upgrade()
        val wsTarget = target.copy(
            signalingUrl = target.signalingUrl.replaceFirst("http", "ws"),
        )

        runBlocking { client().connect(wsTarget) }

        assertEquals(SIGNAL_PATH, requireNotNull(server.takeRequest().requestUrl).encodedPath)
    }

    @Test
    fun `a connection the server never upgrades throws SignalingConnectException`() {
        server.enqueue(MockResponse().setResponseCode(404))
        val client = client()

        assertThrows(SignalingConnectException::class.java) {
            runBlocking { client.connect(target) }
        }
    }

    @Test
    fun `connect is one-shot because the token is one-time use`() {
        upgrade()
        val client = client()
        runBlocking { client.connect(target) }

        assertThrows(IllegalStateException::class.java) {
            runBlocking { client.connect(target) }
        }
    }

    @Test
    fun `the target never renders its token`() {
        assertFalse(target.toString().contains(SECRET_TOKEN))
    }

    // ------------------------------------------------------------------
    // Frame routing
    // ------------------------------------------------------------------

    @Test
    fun `answer, trickled ice, the null end-of-candidates marker, bye and error all route as typed frames`() {
        val serverSide = upgrade()
        val client = client()

        runBlocking {
            client.connect(target)
            client.frames.test {
                val socket = serverSide.awaitSocket()
                socket.send("""{"v": 1, "type": "answer", "payload": {"sdp": "v=0-answer"}}""")
                socket.send(
                    """{"v": 1, "type": "ice", "payload": {"candidate": "candidate:1 1 udp 1 10.0.0.2 50000 typ host", "sdpMid": "0", "sdpMLineIndex": 0}}""",
                )
                socket.send(
                    """{"v": 1, "type": "ice", "payload": {"candidate": null, "sdpMid": null, "sdpMLineIndex": null}}""",
                )
                socket.send("""{"v": 1, "type": "error", "payload": {"code": "AUTH_INVALID_TOKEN", "message": "no"}}""")
                socket.send("""{"v": 1, "type": "bye", "payload": {"reason": "agent_hangup"}}""")

                assertEquals(SignalFrame.Answer("v=0-answer"), awaitItem())
                assertEquals(
                    SignalFrame.RemoteIce("candidate:1 1 udp 1 10.0.0.2 50000 typ host", "0", 0),
                    awaitItem(),
                )
                assertEquals(SignalFrame.RemoteIce(null, null, null), awaitItem())
                assertEquals(SignalFrame.ServerError("AUTH_INVALID_TOKEN", "no"), awaitItem())
                assertEquals(SignalFrame.Bye("agent_hangup"), awaitItem())
                cancelAndIgnoreRemainingEvents()
            }
        }
    }

    @Test
    fun `unknown envelope types are ignored and later frames still route`() {
        val serverSide = upgrade()
        val client = client()

        runBlocking {
            client.connect(target)
            client.frames.test {
                val socket = serverSide.awaitSocket()
                socket.send("""{"v": 1, "type": "transcript.hint", "payload": {"future": true}}""")
                socket.send("""{"v": 1, "type": "answer", "payload": {"sdp": "v=0-after-unknown"}}""")

                assertEquals(SignalFrame.Answer("v=0-after-unknown"), awaitItem())
                cancelAndIgnoreRemainingEvents()
            }
        }
    }

    @Test
    fun `a server-initiated close surfaces as Closed SERVER_CLOSED`() {
        val serverSide = upgrade()
        val client = client()

        runBlocking {
            client.connect(target)
            client.frames.test {
                serverSide.awaitSocket().close(1000, "server done")

                assertEquals(
                    SignalFrame.Closed(SignalingClosedReason.SERVER_CLOSED),
                    awaitItem(),
                )
                cancelAndIgnoreRemainingEvents()
            }
        }
    }

    // ------------------------------------------------------------------
    // Sending
    // ------------------------------------------------------------------

    @Test
    fun `send delivers offer, local ice and bye envelopes to the server`() {
        val serverSide = upgrade()
        val client = client()

        runBlocking { client.connect(target) }
        client.send(OutboundSignal.Offer("v=0-offer"))
        client.send(OutboundSignal.LocalIce("candidate:9 1 udp 9 10.0.0.3 4242 typ host", "0", 0))
        client.send(OutboundSignal.Bye(BYE_REASON_USER_HANGUP))

        // The keepalive loop interleaves pings with these frames — drop them;
        // this test asserts the send() surface, the ping tests own the loop.
        val received = mutableListOf<String>()
        while (received.size < 3) {
            val message = serverSide.awaitMessage()
            if (!message.contains("\"type\":\"ping\"")) received += message
        }
        assertTrue(received[0].contains("\"type\":\"offer\""))
        assertTrue(received[0].contains("v=0-offer"))
        assertTrue(received[1].contains("\"type\":\"ice\""))
        assertTrue(received[2].contains("\"type\":\"bye\""))
        assertTrue(received[2].contains(BYE_REASON_USER_HANGUP))
    }

    @Test
    fun `send after close is a silent drop, never a crash`() {
        upgrade()
        val client = client()
        runBlocking { client.connect(target) }

        client.close()
        client.send(OutboundSignal.Offer("v=0-too-late")) // must not throw
    }

    // ------------------------------------------------------------------
    // Keepalive (docs/13 §6.1: client-driven ping, two misses = dead)
    // ------------------------------------------------------------------

    @Test
    fun `the ping loop keeps sending envelope pings while pongs come back`() {
        val serverSide = upgrade(ServerSide(autoPong = true))
        val client = client(pingIntervalMillis = 100)

        runBlocking { client.connect(target) }

        val pings = List(3) { serverSide.awaitMessage() }
        pings.forEach { ping ->
            assertTrue("expected a ping envelope, got: $ping", ping.contains("\"type\":\"ping\""))
            assertTrue("ping must carry a ts payload", ping.contains("\"ts\":"))
        }
        // Pongs answered every ping — the socket must not have been declared dead.
        assertTrue(client.frames.replayCache.none { it is SignalFrame.Closed })
    }

    @Test
    fun `two unanswered pings mark the socket dead with Closed PING_TIMEOUT`() {
        upgrade(ServerSide(autoPong = false))
        val client = client(pingIntervalMillis = 80)

        runBlocking {
            client.connect(target)
            client.frames.test {
                assertEquals(
                    SignalFrame.Closed(SignalingClosedReason.PING_TIMEOUT),
                    awaitItem(),
                )
                cancelAndIgnoreRemainingEvents()
            }
        }
    }

    @Test
    fun `a locally requested close emits no Closed frame`() {
        upgrade()
        val client = client(pingIntervalMillis = 100)
        runBlocking { client.connect(target) }

        client.close()
        // Give the close handshake time to complete on real threads.
        Thread.sleep(300)

        assertTrue(client.frames.replayCache.none { it is SignalFrame.Closed })
    }

    // ------------------------------------------------------------------
    // Server-side test double: a real WebSocket endpoint
    // ------------------------------------------------------------------

    private class ServerSide(
        private val autoPong: Boolean = false,
    ) : WebSocketListener() {
        private val opened = CountDownLatch(1)
        private val messages = LinkedBlockingQueue<String>()

        @Volatile private var socket: WebSocket? = null

        override fun onOpen(webSocket: WebSocket, response: Response) {
            socket = webSocket
            opened.countDown()
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            if (autoPong && text.contains("\"type\":\"ping\"")) {
                webSocket.send("""{"v": 1, "type": "pong", "payload": {"ts": 0}}""")
                // Pings are consumed by the keepalive assertion queue too.
            }
            messages.add(text)
        }

        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
            // Complete the close handshake for client-initiated closes —
            // without this echo the socket lingers and MockWebServer's
            // shutdown times out with an IOException.
            webSocket.close(code, reason)
        }

        fun awaitSocket(): WebSocket {
            assertTrue("server socket never opened", opened.await(5, TimeUnit.SECONDS))
            return requireNotNull(socket)
        }

        fun awaitMessage(): String {
            val message = messages.poll(5, TimeUnit.SECONDS)
            assertNotNull("no message reached the server in time", message)
            return requireNotNull(message)
        }
    }

    private companion object {
        const val SIGNAL_PATH = "/v1/signal"
        const val SECRET_TOKEN = "st_hV4nQ9pXzL2mR8cT0kWyB6uJ"
    }
}
