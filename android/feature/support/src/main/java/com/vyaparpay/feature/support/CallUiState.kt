package com.vyaparpay.feature.support

import com.vyaparpay.voice.CallState
import com.vyaparpay.voice.EndReason

/**
 * What the merchant is told about the call, collapsed from `:voice`'s
 * [CallState] into the few distinctions a status panel can actually render.
 *
 * Narrower than [CallState] on purpose. `Requesting`/`Signaling`/`Connecting`
 * are three genuinely different failure domains to `:voice` (see [CallState]'s
 * own kdoc — different error copy, different logs) but one spinner to a
 * merchant, so they fold to [CONNECTING] here. The UI layer collapsing them is
 * the right place for that loss: `:voice` keeps the distinction it needs, and
 * this module does not invent three indistinguishable spinner strings.
 */
public enum class CallPhase {
    /** No call. The status panel renders nothing. */
    IDLE,

    /** Tapped through to media-up: session mint, signaling, or ICE. */
    CONNECTING,

    /** Media is live and the `ctx` channel is open. */
    IN_CALL,

    /** Transport dropped; `:voice` is inside its 30 s grace (docs/06 §6). */
    RECONNECTING,

    /** Terminal. [CallUiState.endReason] says why. */
    ENDED,
}

/**
 * A non-blocking message about the call attempt — always advisory, never a
 * state the merchant is stuck in.
 */
public enum class CallNotice {
    /** RECORD_AUDIO denied, but re-askable: tapping Call Support prompts again. */
    MIC_DENIED,

    /** RECORD_AUDIO denied for good. The panel offers app settings. */
    MIC_BLOCKED,

    /**
     * POST_NOTIFICATIONS denied (API 33+). The call runs anyway — see
     * [PermissionManager]'s kdoc — but the merchant is told the ongoing-call
     * notification will not appear, since that notification is normally how
     * they would get back to the call after leaving the app.
     */
    NOTIFICATIONS_DENIED,
}

/**
 * Everything [SupportRoute]'s call surface renders.
 *
 * @param contextFramesDropped screen-context frames the call gave up on
 *   (`CallController.contextFramesDropped`, whose kdoc names this ViewModel as
 *   its intended consumer). Surfaced because `:voice` carries no logger, so a
 *   silently blind agent would otherwise be invisible; non-zero after a call
 *   means part of it was spent reasoning about a stale screen.
 */
public data class CallUiState(
    val phase: CallPhase = CallPhase.IDLE,
    val endReason: EndReason? = null,
    val notice: CallNotice? = null,
    val contextFramesDropped: Long = 0L,
) {
    /** Whether there is anything at all to draw — [CallPhase.IDLE] with no [notice] draws nothing. */
    public val isVisible: Boolean get() = phase != CallPhase.IDLE || notice != null

    /** Whether a hang-up control makes sense: only while a call could still be running. */
    public val canHangUp: Boolean get() = when (phase) {
        CallPhase.CONNECTING, CallPhase.IN_CALL, CallPhase.RECONNECTING -> true
        CallPhase.IDLE, CallPhase.ENDED -> false
    }
}

/** [CallState] -> [CallPhase]. Exhaustive `when`, so a new `CallState` member fails compilation here rather than falling into a wrong bucket. */
internal fun CallState.toPhase(): CallPhase = when (this) {
    CallState.Idle -> CallPhase.IDLE
    CallState.Requesting, CallState.Signaling, CallState.Connecting -> CallPhase.CONNECTING
    CallState.InCall -> CallPhase.IN_CALL
    CallState.Reconnecting -> CallPhase.RECONNECTING
    is CallState.Ended -> CallPhase.ENDED
}
