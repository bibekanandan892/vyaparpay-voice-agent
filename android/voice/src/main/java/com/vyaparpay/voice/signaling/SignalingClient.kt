package com.vyaparpay.voice.signaling

import kotlinx.coroutines.flow.SharedFlow

/**
 * The control plane (docs/03 §3.5): one WebSocket to the voice-worker at
 * `wss://<voice-worker>/v1/signal?session_id=<id>&token=<signaling_token>`,
 * speaking the `{v, type, payload}` envelope (docs/13 §6). It carries SDP and
 * ICE **only** — context, transcripts, and agent state ride the data channel,
 * never this socket (canon ADR-4).
 *
 * This is an interface, not the concrete class the docs sketch, for the same
 * reason the backend puts `SignalingTransport` behind a Protocol: the call
 * controller and its unit tests must run against a fake with no sockets.
 * [OkHttpSignalingClient] is the production implementation.
 */
public interface SignalingClient {

    /**
     * The inbound frames: answer, remote ICE (including the `candidate: null`
     * end-of-candidates marker), bye, server errors, and the terminal
     * [SignalFrame.Closed]. Replay-buffered so a subscriber attaching just
     * after [connect] still sees frames that raced the subscription.
     */
    public val frames: SharedFlow<SignalFrame>

    /**
     * Dial and complete the WS handshake; resumes when the socket is open,
     * throws [SignalingConnectException] when it never opens. One-shot per
     * client instance — the signaling token is one-time use (docs/13 §6.2),
     * so redialing requires a fresh token and a fresh client. (The mid-call
     * reconnect-with-backoff of docs/03 §3.5 builds on that re-mint endpoint
     * and lands with the reconnection task.)
     *
     * Note: an open socket is not yet an authenticated session — a refused
     * token arrives as a [SignalFrame.ServerError] followed by
     * [SignalFrame.Closed], exactly as the server behaves
     * (backend/app/voice/signaling.py `_authenticate`).
     */
    public suspend fun connect(target: SignalingTarget)

    /**
     * Send one client→server frame. Never throws: a frame sent while the
     * socket is closed or closing is dropped — control-plane races against
     * teardown are the norm, and every loss here is recoverable (a lost
     * `bye` is covered by the server's own disconnect handling).
     */
    public fun send(frame: OutboundSignal)

    /**
     * Release the socket (normal close). Does not send `bye` — that is a
     * frame the caller sends first when the teardown is a clean hang-up
     * (docs/13 §6.1). Idempotent; no [SignalFrame.Closed] is emitted for a
     * locally requested close.
     */
    public fun close()
}
