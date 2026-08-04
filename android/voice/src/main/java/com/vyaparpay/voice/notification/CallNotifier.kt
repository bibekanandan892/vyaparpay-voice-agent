package com.vyaparpay.voice.notification

/**
 * The call's ongoing notification (docs/03 §3.3): "An ongoing, non-dismissable
 * call notification with mute and end-call actions ... plus the live call
 * duration."
 *
 * An interface for the same reason [com.vyaparpay.voice.audio.CallAudioSession]
 * is one: `NotificationManager`/`Notification.Builder` are Android framework
 * types absent from a plain JVM test run, so the *policy* — which phase is
 * shown when, and that the notification is posted/cleared the right number of
 * times across the call's lifecycle — is tested against a fake, and
 * [AndroidCallNotifier] is the only class that touches `android.app.Notification`.
 */
public interface CallNotifier {

    /** Post or update the ongoing call notification for [state]. Cheap to call repeatedly with an unchanged state. */
    public fun show(state: CallNotificationState)

    /** Cancel the notification. Idempotent; safe to call when nothing is showing. */
    public fun clear()
}

/** What the notification currently says (docs/03 §3.3). */
public data class CallNotificationState(
    val phase: CallNotificationPhase,
    val muted: Boolean,
)

/** The phases the user-facing notification distinguishes. */
public enum class CallNotificationPhase {

    /** `Requesting`/`Signaling`/`Connecting` — before media is up. */
    CONNECTING,

    /** Media is flowing, `ctx` channel open. */
    IN_CALL,

    /** Transport lost, inside the 30 s grace (docs/06 §6). */
    RECONNECTING,
}
