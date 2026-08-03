package com.vyaparpay.voice.webrtc

import com.vyaparpay.core.network.IceServerDto
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.SharedFlow

/**
 * The media plane (docs/03 §3.4): the one `PeerConnection` per call, the mic
 * track, the `ctx` data channel, and the offer side of the SDP exchange.
 *
 * This is deliberately an *interface over plain Kotlin types* — no org.webrtc
 * symbol appears in any signature. [PeerConnectionWebRtcClient] is the only
 * implementation that touches libwebrtc, and [WebRtcClientFactory] is the only
 * place the native library is loaded, so everything that orchestrates a call
 * (controller, state machine, tests) runs on a plain JVM with fakes.
 *
 * ICE servers arrive as :core:network's [IceServerDto] verbatim — the bundle
 * is "passed verbatim to PeerConnection construction" (docs/13 §2.1), and
 * re-modeling it here would be a second copy of the same contract.
 *
 * Out of scope here, by task boundary: audio focus and routing
 * (`AudioFocusRequest`, earpiece/speaker/Bluetooth — docs/03 §3.4's `route`)
 * belong to the `VoiceCallService` task, which owns the Android audio session
 * around the call.
 */
public interface WebRtcClient {

    /**
     * Media-plane events: local candidates to trickle, connection-state
     * transitions, closure. Replay-buffered — candidates start gathering
     * inside [start], typically before any collector is attached.
     */
    public val events: SharedFlow<RtcEvent>

    /**
     * The `ctx` downlink: raw data-channel payloads (`transcript.*`,
     * `agent.state`, `ctx.request_snapshot` — docs/13 §4). Parsing is the
     * Phase-4 context task's job; this surface just moves bytes.
     */
    public val incoming: Flow<ByteArray>

    /**
     * Bring up the peer: factory-built `PeerConnection` from [iceServers],
     * mic track with echo-cancellation/noise-suppression/auto-gain
     * constraints (docs/06 §2.3), the `ctx` data channel created **before**
     * the offer so SCTP rides the first SDP round trip (docs/03 §3.4), then
     * `createOffer` + `setLocalDescription`. Returns the local offer for the
     * signaling channel. One-shot per client instance — one call, one peer.
     */
    public suspend fun start(iceServers: List<IceServerDto>): Sdp

    /** Apply the server's answer (`setRemoteDescription`). */
    public suspend fun applyAnswer(sdp: Sdp)

    /**
     * Apply one remote `ice` frame. A `null` candidate (the end-of-candidates
     * marker, docs/13 §6) is a no-op — libwebrtc needs no explicit signal,
     * mirroring the server's own handling of the client's marker.
     */
    public fun addRemoteCandidate(candidate: RemoteIceCandidate)

    /**
     * ICE restart after a network change: a fresh offer with new credentials
     * on the same peer (docs/13 §6.1). The caller re-sends it as a plain
     * `offer` frame over the signaling channel.
     */
    public suspend fun restartIce(): Sdp

    /** Mute is track-level: the mic track stops producing, the connection stays up. */
    public fun setMuted(muted: Boolean)

    /** Send one client→server payload on the `ctx` channel (reliable + ordered, docs/13 §4). */
    public suspend fun sendContext(bytes: ByteArray)

    /**
     * Tear down everything this client owns — channel, tracks, peer, factory
     * resources. Idempotent; a leaked `PeerConnection` is a live mic
     * (docs/03 §2.2).
     */
    public suspend fun close()
}
