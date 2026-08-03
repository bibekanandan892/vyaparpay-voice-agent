package com.vyaparpay.voice.signaling

/**
 * The typed frames [SignalingClient] delivers and accepts (docs/03 §3.5's
 * `SignalFrame` surface), split into two sealed hierarchies so the compiler —
 * not a runtime check — enforces direction: a client can never `send` an
 * [SignalFrame.Answer], and [SignalingClient.frames] can never emit an offer.
 */

/** What a masked secret renders as in `toString()` — matches :core:network. */
private const val REDACTED: String = "<redacted>"

/** Server→client frames, emitted on [SignalingClient.frames]. */
public sealed interface SignalFrame {

    /** The server's SDP answer to the current offer (docs/13 §6). */
    public data class Answer(val sdp: String) : SignalFrame

    /**
     * One server-trickled ICE candidate. A `null` [candidate] is the
     * end-of-candidates marker (docs/13 §6): aiortc embeds every candidate in
     * the answer SDP and sends exactly one null marker after it
     * (backend/app/voice/peer_session.py judgment call 3), so the client must
     * tolerate both the embedded-only shape and genuinely trickled candidates.
     */
    public data class RemoteIce(
        val candidate: String?,
        val sdpMid: String?,
        val sdpMLineIndex: Int?,
    ) : SignalFrame

    /** Clean teardown from the server — `bye` is bidirectional (docs/13 §6). */
    public data class Bye(val reason: String) : SignalFrame

    /** A server `error` frame; [code] is the docs/13 §1.1 vocabulary. */
    public data class ServerError(val code: String, val message: String) : SignalFrame

    /**
     * The WebSocket died — server close, transport failure, or two missed
     * pongs (docs/13 §6: "two missed pongs mark the WS dead"). Emitted at most
     * once, and never for a close the client itself requested.
     */
    public data class Closed(val reason: SignalingClosedReason) : SignalFrame
}

/** Why the signaling socket ended without the client asking it to. */
public enum class SignalingClosedReason {
    /** The server completed a WS close handshake. */
    SERVER_CLOSED,

    /** The TCP/TLS transport failed — the mobile-network norm (docs/06 §8). */
    TRANSPORT_FAILURE,

    /** Two consecutive 10 s envelope pings went unanswered (docs/13 §6). */
    PING_TIMEOUT,
}

/** Client→server frames, accepted by [SignalingClient.send]. */
public sealed interface OutboundSignal {

    /** The SDP offer — initial, or an ICE-restart re-offer (docs/13 §6.1). */
    public data class Offer(val sdp: String) : OutboundSignal

    /** One locally gathered ICE candidate, trickled the moment it is found (docs/06 §2). */
    public data class LocalIce(
        val candidate: String,
        val sdpMid: String?,
        val sdpMLineIndex: Int,
    ) : OutboundSignal

    /** Clean teardown: `user_hangup` from the in-call UI (docs/13 §6). */
    public data class Bye(val reason: String) : OutboundSignal
}

/** The `reason` the client sends when the user ends the call (docs/13 §6). */
public const val BYE_REASON_USER_HANGUP: String = "user_hangup"

/**
 * Where and as whom to connect: the connect bundle's signaling coordinates
 * (docs/13 §2.1). The token is live bearer material with a 5-minute, one-time
 * life (docs/13 §6.2) — masked in [toString] exactly like
 * `ConnectBundleDto.signalingToken`, so an accidental log of a target is inert.
 */
public data class SignalingTarget(
    /** `wss://` URL of the voice-worker's `/v1/signal` (docs/13 §6). */
    val signalingUrl: String,
    val sessionId: String,
    /** One-time signaling token — never logged, masked here. */
    val token: String,
) {
    override fun toString(): String =
        "SignalingTarget(signalingUrl=$signalingUrl, sessionId=$sessionId, token=$REDACTED)"
}

/**
 * The WS never reached the open state — DNS/TCP/TLS failure or a non-101
 * response. Distinct from a post-open [SignalFrame.ServerError]: the
 * signaling token is one-time use, so the caller must not blindly redial
 * with the same credentials (docs/03 §3.5).
 */
public class SignalingConnectException(
    message: String,
    cause: Throwable? = null,
) : Exception(message, cause)
