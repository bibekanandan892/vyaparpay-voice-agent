package com.vyaparpay.voice

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * The call's single source of truth (docs/03 §3.2): a pure state reducer —
 * events in, `(state, side-effects)` out — with no `org.webrtc` and no
 * Android imports, so every transition is exhaustively unit-testable.
 *
 * Concurrency contract: [dispatch] is **not** internally synchronized. All
 * callers must funnel through one serial executor — [CallController] marshals
 * every source (libwebrtc's signaling thread, OkHttp's reader thread, UI
 * intents) through a single channel, exactly the docs/03 §3.2 threading rule.
 *
 * Unmatched (state, event) pairs are deliberate no-ops, not errors: terminal
 * races are constant in this domain (a `bye` racing a hang-up, a candidate
 * landing after teardown, a mint completing after the user gave up), and
 * every one of them must converge on the state already reached — the client
 * twin of the server's "every teardown path converges" rule.
 */
public class CallStateMachine {

    private val _state = MutableStateFlow<CallState>(CallState.Idle)
    public val state: StateFlow<CallState> = _state.asStateFlow()

    /** Apply one event; returns the effects the executor must run, in order. */
    public fun dispatch(event: CallEvent): List<CallEffect> {
        val (next, effects) = reduce(_state.value, event)
        _state.value = next
        return effects
    }

    private fun reduce(
        state: CallState,
        event: CallEvent,
    ): Pair<CallState, List<CallEffect>> = when (state) {
        CallState.Idle -> when (event) {
            CallEvent.SupportTapped ->
                CallState.Requesting to listOf(CallEffect.MintSession)
            else -> stay(state)
        }

        CallState.Requesting -> when (event) {
            is CallEvent.SessionMinted ->
                CallState.Signaling to listOf(CallEffect.OpenTransport(event.bundle))
            is CallEvent.Failed ->
                // 429/503/network error: nothing to tear down — no session
                // row, no token, nothing on the voice-worker (docs/13 §2.1).
                CallState.Ended(EndReason.SETUP_FAILED) to emptyList()
            CallEvent.UserHungUp ->
                // Gave up before the bundle arrived; ReleaseCall cancels the
                // in-flight mint so a late 201 cannot resurrect the attempt.
                CallState.Ended(EndReason.USER_HUNG_UP) to listOf(CallEffect.ReleaseCall)
            else -> stay(state)
        }

        CallState.Signaling -> when (event) {
            CallEvent.AnswerApplied ->
                // Control plane done; trickle ICE and DTLS continue — no
                // effect needed, the transport is already running.
                CallState.Connecting to emptyList()
            is CallEvent.Failed ->
                // WS refused / error frame / answer timeout: dispose the
                // half-built peer (docs/03 §3.2).
                CallState.Ended(EndReason.SETUP_FAILED) to listOf(CallEffect.ReleaseCall)
            CallEvent.SignalingClosed ->
                CallState.Ended(EndReason.SETUP_FAILED) to listOf(CallEffect.ReleaseCall)
            is CallEvent.RemoteBye ->
                CallState.Ended(EndReason.REMOTE_HUNG_UP) to listOf(CallEffect.ReleaseCall)
            CallEvent.UserHungUp -> userHangUp()
            else -> stay(state)
        }

        CallState.Connecting -> when (event) {
            CallEvent.PeerConnected ->
                // Media up, `ctx` open — bind the capture pipeline to it
                // (docs/03 §3.2, §3.10). Audio focus is deliberately NOT an
                // effect here: VoiceCallCoordinator acquires it off the
                // *state* the moment the service must be in the foreground
                // (Requesting), strictly before WebRtcClient.start() opens the
                // mic — see that class's own kdoc. Only the context binding
                // genuinely belongs on this edge, because it is the edge on
                // which the `ctx` data channel first becomes usable.
                CallState.InCall to listOf(CallEffect.BindContextPublisher)
            is CallEvent.Failed ->
                // ICE failed / DTLS timeout — the media plane never came up.
                CallState.Ended(EndReason.SETUP_FAILED) to listOf(CallEffect.ReleaseCall)
            CallEvent.TransportLost ->
                // libwebrtc reports an initial-connect ICE/DTLS failure as
                // PeerConnectionState.FAILED, which reaches the reducer as
                // the same TransportLost used for post-connect losses. Here
                // there is no call to salvage — a grace timer would leave a
                // stuck Connecting call holding a live mic (docs/03 §2.2's
                // "a leaked PeerConnection is a live mic"), so it is a setup
                // failure, the docs/03 §3.2 Connecting → Ended row.
                CallState.Ended(EndReason.SETUP_FAILED) to listOf(CallEffect.ReleaseCall)
            CallEvent.SignalingClosed ->
                CallState.Ended(EndReason.SETUP_FAILED) to listOf(CallEffect.ReleaseCall)
            is CallEvent.RemoteBye ->
                CallState.Ended(EndReason.REMOTE_HUNG_UP) to listOf(CallEffect.ReleaseCall)
            CallEvent.UserHungUp -> userHangUp()
            else -> stay(state)
        }

        CallState.InCall -> when (event) {
            CallEvent.UserHungUp -> userHangUp()
            is CallEvent.RemoteBye ->
                // The server already ended its side — close ours; the DELETE
                // would be a no-op against an ending session.
                CallState.Ended(EndReason.REMOTE_HUNG_UP) to listOf(CallEffect.ReleaseCall)
            CallEvent.TransportLost ->
                CallState.Reconnecting to listOf(CallEffect.StartReconnectGrace)
            CallEvent.SignalingClosed ->
                // Media may still flow (the WS is control plane only,
                // docs/13 §6.1), but without it there is no ICE restart and
                // no clean bye. Until the docs/03 §3.5 backoff-reconnect task
                // lands, the honest behavior is the bounded grace — recover
                // or end, never a zombie control plane.
                CallState.Reconnecting to listOf(CallEffect.StartReconnectGrace)
            else -> stay(state)
        }

        CallState.Reconnecting -> when (event) {
            CallEvent.TransportResumed ->
                CallState.InCall to listOf(CallEffect.CancelReconnectGrace)
            CallEvent.GraceExpired ->
                // docs/06 §6: tear down; the next attempt mints a fresh
                // session. The DELETE lets the backend finalize early instead
                // of waiting out its own grace — idempotent either way.
                CallState.Ended(EndReason.GRACE_EXPIRED) to
                    listOf(CallEffect.ReleaseCall, CallEffect.EndSessionOnApi)
            CallEvent.UserHungUp -> userHangUp()
            is CallEvent.RemoteBye ->
                CallState.Ended(EndReason.REMOTE_HUNG_UP) to listOf(CallEffect.ReleaseCall)
            else -> stay(state)
        }

        is CallState.Ended -> stay(state)
    }

    /** The one hang-up shape (docs/03 §3.2): send `bye`, close both planes, `DELETE /v1/sessions/{id}`. */
    private fun userHangUp(): Pair<CallState, List<CallEffect>> =
        CallState.Ended(EndReason.USER_HUNG_UP) to listOf(
            CallEffect.SendBye(BYE_REASON_USER_HANGUP_EVENT),
            CallEffect.ReleaseCall,
            CallEffect.EndSessionOnApi,
        )

    private fun stay(state: CallState): Pair<CallState, List<CallEffect>> =
        state to emptyList()

    private companion object {
        /** Mirrors `signaling.BYE_REASON_USER_HANGUP` without importing the transport package into a pure reducer. */
        private const val BYE_REASON_USER_HANGUP_EVENT: String = "user_hangup"
    }
}
