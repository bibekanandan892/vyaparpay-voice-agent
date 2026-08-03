package com.vyaparpay.voice

import com.vyaparpay.core.network.ApiError
import com.vyaparpay.core.network.ConnectBundleDto

/**
 * Everything that can happen to a call (docs/03 §3.2's event vocabulary),
 * from every source — UI intents, the session API, signaling frames, and
 * media-plane transitions — normalized into one type so the reducer sees a
 * single ordered stream.
 */
public sealed interface CallEvent {

    /** The user tapped Call Support (permissions already granted, docs/03 §3.6). */
    public data object SupportTapped : CallEvent

    /** `POST /v1/sessions` returned the connect bundle (docs/13 §2.1). */
    public data class SessionMinted(val bundle: ConnectBundleDto) : CallEvent

    /** The server's answer applied cleanly — control plane done, media plane next (docs/03 §3.2). */
    public data object AnswerApplied : CallEvent

    /** `PeerConnectionState.CONNECTED`: ICE pair nominated, DTLS-SRTP up, `ctx` open. */
    public data object PeerConnected : CallEvent

    /** The media path died under the call — ICE `disconnected`/`failed` (docs/06 §8). */
    public data object TransportLost : CallEvent

    /** Media re-established on a new candidate pair. */
    public data object TransportResumed : CallEvent

    /** The 30 s reconnect grace elapsed (docs/06 §6). */
    public data object GraceExpired : CallEvent

    /**
     * A setup stage failed: session mint refused, WS refused, a server
     * `error` frame (its docs/13 §1.1 code mapped via [ApiError.fromWireCode]),
     * an answer that never came, or media that never connected.
     */
    public data class Failed(val error: ApiError) : CallEvent

    /** The user tapped End. */
    public data object UserHungUp : CallEvent

    /** The server sent `bye` (docs/13 §6 — bye is bidirectional). */
    public data class RemoteBye(val reason: String) : CallEvent

    /**
     * The signaling WebSocket died without the client asking (server close,
     * transport failure, or two missed pongs). Deliberately distinct from
     * [Failed]: what it means depends on the phase — fatal while the call is
     * being established, a bounded-grace recovery case once media is up
     * (docs/03 §7) — and that decision belongs to the reducer, not the
     * collector that observed the drop.
     */
    public data object SignalingClosed : CallEvent
}
