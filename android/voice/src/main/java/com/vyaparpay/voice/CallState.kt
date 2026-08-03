package com.vyaparpay.voice

/**
 * The call's single source of truth (docs/03-android-architecture.md §3.2).
 *
 * `Signaling` and `Connecting` are deliberately distinct states even though a
 * user sees one "connecting…" spinner, because they fail differently:
 * `Signaling` is the control plane coming up (WS connect, offer sent, answer
 * applied) and a failure there means the voice-worker refused or never
 * answered; `Connecting` is the media plane (ICE checks, DTLS-SRTP) and a
 * failure there means NAT traversal broke. Different error copy, different logs.
 *
 * The reducer that drives these transitions — pure, with no `org.webrtc` and no
 * Android imports so it stays exhaustively unit-testable — lands with
 * `CallStateMachine` alongside `VoiceCallService`.
 */
public sealed interface CallState {

    public data object Idle : CallState

    /** `POST /v1/sessions` is in flight. */
    public data object Requesting : CallState

    /** Control plane: WS connected, offer sent, waiting on the answer. */
    public data object Signaling : CallState

    /** Media plane: candidate pairs checking, DTLS-SRTP handshaking. */
    public data object Connecting : CallState

    /** Media is up, the `ctx` channel is open, audio focus is held. */
    public data object InCall : CallState

    /** Transport lost; an ICE restart is in flight, bounded by the 30 s grace. */
    public data object Reconnecting : CallState

    public data class Ended(val reason: EndReason) : CallState
}

/** Why a call stopped — the thing the summary card and the logs both key off. */
public enum class EndReason {
    /** The user tapped End. */
    USER_HUNG_UP,

    /** The server sent `bye`. */
    REMOTE_HUNG_UP,

    /** Setup never completed — session refused, WS refused, or ICE failed. */
    SETUP_FAILED,

    /** The 30 s reconnect grace elapsed. */
    GRACE_EXPIRED,
}

/**
 * Whether this state represents an established call worth restoring —
 * the docs/03 §7 process-death question, not "does a `PeerConnection`
 * object exist" (one is created as early as `Signaling`, but a call that
 * never reached media is redialed from scratch, never resumed).
 *
 * The system's rule, translated to the client: end the call honestly, never
 * fake a live one. The overlay must never show a listening indicator over a
 * call that no longer exists (docs/03 §7).
 */
public val CallState.holdsLiveCall: Boolean
    get() = when (this) {
        CallState.InCall -> true
        CallState.Reconnecting -> true
        CallState.Idle -> false
        CallState.Requesting -> false
        CallState.Signaling -> false
        CallState.Connecting -> false
        is CallState.Ended -> false
    }

/**
 * Whether the foreground service should be running.
 *
 * True from the moment the user taps, not from the moment media connects: a
 * mic-type FGS on Android 14+ has to start from a guaranteed-foreground context
 * with `RECORD_AUDIO` already granted, and the tap is that moment (docs/03 §3.3).
 */
public val CallState.requiresForegroundService: Boolean
    get() = when (this) {
        CallState.Idle -> false
        is CallState.Ended -> false
        CallState.Requesting -> true
        CallState.Signaling -> true
        CallState.Connecting -> true
        CallState.InCall -> true
        CallState.Reconnecting -> true
    }
