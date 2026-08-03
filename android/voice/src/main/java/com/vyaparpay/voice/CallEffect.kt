package com.vyaparpay.voice

import com.vyaparpay.core.network.ConnectBundleDto

/**
 * What a transition asks the outside world to do (docs/03 §3.2: the reducer
 * returns `(state, side-effects)`; the effects are *executed* elsewhere —
 * today by [CallController], ultimately under `VoiceCallService`). Keeping
 * them as data keeps the reducer pure and the effect list assertable in
 * tests.
 *
 * Ordering inside one dispatch's effect list is meaningful and preserved by
 * the executor — e.g. [SendBye] precedes [ReleaseCall] so the farewell frame
 * leaves on a socket that still exists.
 */
public sealed interface CallEffect {

    /** `POST /v1/sessions` with the current screen context (docs/13 §2.1). */
    public data object MintSession : CallEffect

    /**
     * Bring up both planes from the connect bundle: signaling WS (session_id
     * + one-time token), peer + mic + `ctx` channel, offer out, trickle ICE
     * both directions (docs/03 §3.2's `Requesting → Signaling` effects).
     */
    public data class OpenTransport(val bundle: ConnectBundleDto) : CallEffect

    /** Send `bye {reason}` on the still-open signaling socket (docs/13 §6). */
    public data class SendBye(val reason: String) : CallEffect

    /** Arm the 30 s reconnect grace timer (docs/06 §6). */
    public data object StartReconnectGrace : CallEffect

    /** Disarm the grace timer — the transport came back. */
    public data object CancelReconnectGrace : CallEffect

    /** `DELETE /v1/sessions/{id}` — idempotent by contract (docs/13 §2.2). */
    public data object EndSessionOnApi : CallEffect

    /**
     * Tear down whatever transport exists: cancel timers and collectors,
     * close the peer (a leaked `PeerConnection` is a live mic, docs/03 §2.2),
     * close the socket. Safe when nothing was ever opened — every terminal
     * path converges here, like the server's one `finally` block.
     */
    public data object ReleaseCall : CallEffect
}
