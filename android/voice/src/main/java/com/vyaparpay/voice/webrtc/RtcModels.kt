package com.vyaparpay.voice.webrtc

/**
 * The framework-free vocabulary of the media plane. Every type here is plain
 * Kotlin on purpose: [WebRtcClient] is an interface over these, so the call
 * controller, the state machine, and every JVM unit test compile and run
 * without org.webrtc on the runtime path (docs/03 §3.4's isolation rule —
 * the client-side twin of the backend's "only PeerSession imports aiortc").
 */

/** One SDP blob — offer or answer. A value class so it can never be confused with a candidate string. */
@JvmInline
public value class Sdp(public val value: String)

/** A locally gathered ICE candidate, ready to trickle out as an `ice` frame (docs/13 §6). */
public data class LocalIceCandidate(
    val candidate: String,
    val sdpMid: String?,
    val sdpMLineIndex: Int,
)

/**
 * A server-trickled candidate from an inbound `ice` frame. [candidate] is
 * `null` for the end-of-candidates marker (docs/13 §6) — the server embeds its
 * candidates in the answer SDP and marks the end with one null
 * (backend/app/voice/peer_session.py judgment call 3), so a null here is a
 * routine no-op, never an error.
 */
public data class RemoteIceCandidate(
    val candidate: String?,
    val sdpMid: String?,
    val sdpMLineIndex: Int?,
)

/** What the media plane reports upward (docs/03 §3.4's `events` surface). */
public sealed interface RtcEvent {

    /** Trickle this out now — media starts on the first working pair (docs/06 §2). */
    public data class IceCandidateFound(val candidate: LocalIceCandidate) : RtcEvent

    /** `PeerConnectionState.CONNECTED` for the first time: ICE pair nominated, DTLS-SRTP up (docs/03 §3.2). */
    public data object PeerConnected : RtcEvent

    /** ICE `disconnected`/`failed` — the media path died under the call (docs/06 §8). */
    public data object TransportLost : RtcEvent

    /** Connected again after a loss — a new candidate pair carried the call back. */
    public data object TransportResumed : RtcEvent

    /** The peer connection is gone for good. */
    public data object Closed : RtcEvent
}

/** A media-plane operation failed — factory, offer, answer, or channel trouble. */
public class WebRtcException(
    message: String,
    cause: Throwable? = null,
) : Exception(message, cause)
