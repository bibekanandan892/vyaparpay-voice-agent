package com.vyaparpay.feature.support

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import com.vyaparpay.core.analytics.EventTracker
import com.vyaparpay.voice.EndReason

/** testTag for the call status surface as a whole. */
internal const val CALL_PANEL_TEST_TAG = "call_status_panel"

/** testTag for the call-state line ("Connecting…", "Connected", …). */
internal const val CALL_STATUS_TEST_TAG = "call_status_text"

/** testTag for the End Call control. */
internal const val CALL_HANG_UP_TEST_TAG = "call_hang_up_cta"

/** testTag for the dismiss control shown once a call is over. */
internal const val CALL_DISMISS_TEST_TAG = "call_dismiss_secondary"

/** testTag for the "open app settings" control offered on a permanent mic denial. */
internal const val CALL_SETTINGS_TEST_TAG = "call_settings_secondary"

/**
 * The minimum honest surface for a running call: what state it is in, and a
 * way out of it.
 *
 * **This is deliberately not docs/03 §3.12's `ConversationOverlay`, and must
 * not grow into it here.** The overlay's defining feature is live transcript
 * and agent-state rendering, and those frames are a *documented open seam*:
 * `ContextDownlink.classify` folds `transcript.partial`/`transcript.final`/
 * `agent.state` to `IGNORED`, and `CallController.pumpContextDownlink`'s kdoc
 * names consuming them as the overlay task's job, explicitly left undone so
 * this task stays reviewable. Adding transcript UI here would mean opening
 * that seam — a second `webRtc.incoming` collector and a downlink vocabulary —
 * which is a different change with a different blast radius. What is here
 * covers the trigger path end to end: start a call, watch it connect, end it.
 *
 * **Placement.** [SupportRoute] layers this as a bottom-aligned banner over
 * `HelpScreen` rather than threading it into that screen's scroll column. The
 * banner position keeps `HelpScreen`'s layout and every one of its existing
 * tests untouched, and it is the conventional spot for an ongoing-call
 * affordance. It is not a modal scrim: the FAQ list stays readable and
 * scrollable underneath, because a merchant on a support call is frequently
 * being walked through the very content behind it.
 */
@Composable
internal fun CallStatusPanel(
    state: CallUiState,
    events: EventTracker,
    onHangUp: () -> Unit,
    onDismiss: () -> Unit,
    onOpenSettings: () -> Unit,
    modifier: Modifier = Modifier,
) {
    if (!state.isVisible) return

    Surface(
        modifier = modifier
            .fillMaxWidth()
            .testTag(CALL_PANEL_TEST_TAG),
        color = MaterialTheme.colorScheme.surfaceVariant,
        contentColor = MaterialTheme.colorScheme.onSurfaceVariant,
        shape = MaterialTheme.shapes.medium,
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Text(
                text = state.statusLine(),
                style = MaterialTheme.typography.titleSmall,
                modifier = Modifier.testTag(CALL_STATUS_TEST_TAG),
            )

            state.notice?.let { notice ->
                Text(text = notice.message(), style = MaterialTheme.typography.bodySmall)
            }

            // Only after a call, and only when the agent actually lost frames.
            // CallController.contextFramesDropped's kdoc: non-zero means part
            // of the call was spent reasoning about a stale screen, and :voice
            // has no logger to say so anywhere else.
            if (state.phase == CallPhase.ENDED && state.contextFramesDropped > 0L) {
                Text(
                    text = "Screen updates missed during this call: ${state.contextFramesDropped}",
                    style = MaterialTheme.typography.bodySmall,
                )
            }

            if (state.canHangUp) {
                SupportButton(
                    label = "End Call",
                    testTag = CALL_HANG_UP_TEST_TAG,
                    filled = true,
                    events = events,
                    onClick = onHangUp,
                )
            } else {
                if (state.notice == CallNotice.MIC_BLOCKED) {
                    SupportButton(
                        label = "Open Settings",
                        testTag = CALL_SETTINGS_TEST_TAG,
                        filled = true,
                        events = events,
                        onClick = onOpenSettings,
                    )
                }
                SupportButton(
                    label = "Dismiss",
                    testTag = CALL_DISMISS_TEST_TAG,
                    filled = false,
                    events = events,
                    onClick = onDismiss,
                )
            }
        }
    }
}

/**
 * The one line describing the call.
 *
 * [CallPhase.IDLE] with a notice attached is the permission-denial case: there
 * is no call to describe, so the notice text below carries the whole message
 * and this line names the thing that did not happen.
 */
private fun CallUiState.statusLine(): String = when (phase) {
    CallPhase.IDLE -> "Support call"
    CallPhase.CONNECTING -> "Connecting to support…"
    CallPhase.IN_CALL -> "Connected to support"
    CallPhase.RECONNECTING -> "Reconnecting…"
    CallPhase.ENDED -> endReason.endedLine()
}

/**
 * `null` covers the ends `:voice` reports without a reason reaching this
 * layer — a dead service process, or a service that was bound but never had a
 * call (see `CallViewModel.onBound`). "Call ended" is the honest thing to say
 * about all of them: something finished, and we will not invent a cause.
 */
private fun EndReason?.endedLine(): String = when (this) {
    EndReason.USER_HUNG_UP -> "Call ended"
    EndReason.REMOTE_HUNG_UP -> "Support ended the call"
    EndReason.SETUP_FAILED -> "Couldn't connect to support"
    EndReason.GRACE_EXPIRED -> "Call dropped — connection lost"
    null -> "Call ended"
}

private fun CallNotice.message(): String = when (this) {
    CallNotice.MIC_DENIED ->
        "Support calls need your microphone. Tap Call Support to allow it."
    CallNotice.MIC_BLOCKED ->
        "Microphone access is turned off for VyaparPay. Turn it on in Settings to call support, " +
            "or use \"Chat with us instead\"."
    CallNotice.NOTIFICATIONS_DENIED ->
        "Notifications are off, so you won't see an ongoing-call reminder. The call still works."
}
