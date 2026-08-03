package com.vyaparpay.voice.signaling

import java.util.concurrent.atomic.AtomicBoolean
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.launch
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString

/**
 * The production [SignalingClient]: one OkHttp WebSocket, the docs/13 §6
 * envelope via [SignalWire], and the client-driven keepalive of §6.1 — an
 * envelope `ping` every 10 s, `pong` expected before the next ping, two
 * misses marking the socket dead.
 *
 * Judgment calls, flagged per house style:
 *
 * 1. **The token never appears in a log or a `toString`.** The query string is
 *    built once in [buildUrl] and handed straight to OkHttp; nothing in this
 *    class logs the URL, the request, or the [SignalingTarget] (whose own
 *    `toString` masks the token) — the same redaction discipline as
 *    :core:network's `ConnectBundleDto` and the backend's judgment call 3.
 * 2. **Envelope pings, not WS protocol pings** (docs/03 §3.5): they round-trip
 *    through the voice-worker's session routing, so they prove the *session*
 *    is alive, not merely the TCP path. The interval is a constructor
 *    parameter purely so tests can compress time — production uses the
 *    docs-fixed 10 s.
 * 3. **[frames] carries a replay buffer.** OkHttp delivers frames on its
 *    reader thread the instant the socket opens; the controller subscribes
 *    from a coroutine. Replay makes that race unlosable instead of carefully
 *    ordered — the same reasoning as the backend accepting frames only after
 *    wiring the peer.
 * 4. **Exactly one terminal emission, and none for a local [close].** Server
 *    close, transport failure, and ping timeout converge on [emitTerminal];
 *    a close the client itself requested is not news the controller needs.
 * 5. **A server `ping` is answered with a `pong`.** The demo server never
 *    initiates pings (its judgment call 7), but the §6 table marks the type
 *    bidirectional, and answering costs one line.
 */
public class OkHttpSignalingClient(
    private val okHttp: OkHttpClient,
    private val scope: CoroutineScope,
    private val pingIntervalMillis: Long = PING_INTERVAL_MILLIS,
    private val clock: () -> Long = System::currentTimeMillis,
) : SignalingClient {

    private val _frames = MutableSharedFlow<SignalFrame>(
        replay = FRAME_REPLAY,
        extraBufferCapacity = FRAME_BUFFER,
        onBufferOverflow = BufferOverflow.DROP_OLDEST,
    )
    override val frames: SharedFlow<SignalFrame> = _frames.asSharedFlow()

    private val connectCalled = AtomicBoolean(false)
    private val locallyClosed = AtomicBoolean(false)
    private val terminalEmitted = AtomicBoolean(false)

    @Volatile private var webSocket: WebSocket? = null
    @Volatile private var pingJob: Job? = null
    @Volatile private var pongSinceLastPing = false

    override suspend fun connect(target: SignalingTarget) {
        check(connectCalled.compareAndSet(false, true)) {
            "connect() is one-shot: the signaling token is one-time use (docs/13 §6.2)"
        }
        val request = Request.Builder().url(buildUrl(target)).build()
        val opened = CompletableDeferred<Unit>()
        val socket = okHttp.newWebSocket(request, FrameListener(opened))
        webSocket = socket
        try {
            opened.await()
        } catch (e: CancellationException) {
            socket.cancel()
            throw e
        }
        pingJob = scope.launch { pingLoop(socket) }
    }

    override fun send(frame: OutboundSignal) {
        // Dropped-when-closed by contract; WebSocket.send already returns
        // false instead of throwing once the socket is closing.
        webSocket?.send(SignalWire.encode(frame))
    }

    override fun close() {
        locallyClosed.set(true)
        pingJob?.cancel()
        webSocket?.close(WS_NORMAL_CLOSE, "client closed")
    }

    /**
     * `wss`/`ws` → `https`/`http` for OkHttp's URL model (the scheme swap
     * OkHttp itself performs for string URLs), then the two auth query
     * parameters of docs/13 §6. Never logged (judgment call 1).
     */
    private fun buildUrl(target: SignalingTarget): HttpUrl {
        val httpish = when {
            target.signalingUrl.startsWith("wss:", ignoreCase = true) ->
                "https:" + target.signalingUrl.substring("wss:".length)
            target.signalingUrl.startsWith("ws:", ignoreCase = true) ->
                "http:" + target.signalingUrl.substring("ws:".length)
            else -> target.signalingUrl
        }
        return httpish.toHttpUrl().newBuilder()
            .addQueryParameter(QUERY_SESSION_ID, target.sessionId)
            .addQueryParameter(QUERY_TOKEN, target.token)
            .build()
    }

    private suspend fun pingLoop(socket: WebSocket) {
        var missedPongs = 0
        while (true) {
            pongSinceLastPing = false
            socket.send(SignalWire.encodePing(clock()))
            delay(pingIntervalMillis)
            if (pongSinceLastPing) missedPongs = 0 else missedPongs++
            if (missedPongs >= MAX_MISSED_PONGS) {
                // docs/13 §6: two missed pongs mark the WS dead. Emit first,
                // then cancel — cancel() fires onFailure, which finds the
                // terminal flag already set.
                emitTerminal(SignalFrame.Closed(SignalingClosedReason.PING_TIMEOUT))
                socket.cancel()
                return
            }
        }
    }

    private fun emitTerminal(frame: SignalFrame.Closed) {
        if (terminalEmitted.compareAndSet(false, true) && !locallyClosed.get()) {
            _frames.tryEmit(frame)
        }
    }

    private inner class FrameListener(
        private val opened: CompletableDeferred<Unit>,
    ) : WebSocketListener() {

        override fun onOpen(webSocket: WebSocket, response: Response) {
            opened.complete(Unit)
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            when (val decoded = SignalWire.decode(text)) {
                is SignalWire.Decoded.Frame -> _frames.tryEmit(decoded.frame)
                is SignalWire.Decoded.PingFromServer ->
                    webSocket.send(SignalWire.encodePong(decoded.ts))
                is SignalWire.Decoded.Pong -> pongSinceLastPing = true
                is SignalWire.Decoded.Ignored -> Unit // docs/13 §9
            }
        }

        override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
            // Signaling frames are JSON text by contract — the server rejects
            // binary the same way (backend judgment: "frames must be JSON
            // text"). Ignore rather than error: one junk frame must not cost
            // the control plane.
        }

        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
            // Acknowledge the server's close handshake; onClosed follows.
            webSocket.close(code, reason)
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            pingJob?.cancel()
            emitTerminal(SignalFrame.Closed(SignalingClosedReason.SERVER_CLOSED))
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            // Pre-open: surface on connect(). completeExceptionally returns
            // false if the deferred already completed — i.e. this is a
            // post-open transport death, which is Closed, not a connect error.
            if (opened.completeExceptionally(
                    SignalingConnectException("signaling WebSocket connect failed", t),
                )
            ) {
                return
            }
            pingJob?.cancel()
            emitTerminal(SignalFrame.Closed(SignalingClosedReason.TRANSPORT_FAILURE))
        }
    }

    public companion object {
        /** docs/13 §6.1: the client-driven keepalive cadence. */
        public const val PING_INTERVAL_MILLIS: Long = 10_000L

        /** docs/13 §6: "two missed pongs mark the WS dead". */
        public const val MAX_MISSED_PONGS: Int = 2

        private const val QUERY_SESSION_ID: String = "session_id"
        private const val QUERY_TOKEN: String = "token"
        private const val WS_NORMAL_CLOSE: Int = 1000

        private const val FRAME_REPLAY: Int = 32
        private const val FRAME_BUFFER: Int = 32
    }
}
