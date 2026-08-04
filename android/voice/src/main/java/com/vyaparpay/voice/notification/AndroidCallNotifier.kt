package com.vyaparpay.voice.notification

import android.Manifest
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import androidx.core.app.NotificationChannelCompat
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import com.vyaparpay.voice.R
import com.vyaparpay.voice.service.VoiceCallService

/**
 * The one class in `:voice` that touches `android.app.Notification`
 * (docs/03 §3.3): builds the ongoing call notification, its mute/end actions,
 * and the tap-to-return `PendingIntent`.
 *
 * **Tap-to-return without depending on `:app`.** `:voice → :core:* only`
 * (docs/03 §1) forbids a compile-time reference to `MainActivity`, which lives
 * in `:app`. `packageManager.getLaunchIntentForPackage` resolves the app's own
 * launcher activity by package name instead — the standard cross-module
 * pattern for exactly this shape of constraint, and it needs no edge the
 * dependency-rule check would reject.
 *
 * **POST_NOTIFICATIONS is soft** (docs/03 §3.6): [show] checks the grant
 * before calling through to `NotificationManagerCompat`, so a denial is a
 * silent no-op — the call keeps running headless, controlled from the in-app
 * overlay — rather than a crash or a permission-rationale surprise from a
 * background service.
 */
public class AndroidCallNotifier(
    private val context: Context,
) : CallNotifier {

    private val manager = NotificationManagerCompat.from(context)

    init {
        val channel = NotificationChannelCompat.Builder(CHANNEL_ID, NotificationManagerCompat.IMPORTANCE_LOW)
            .setName(context.getString(R.string.voice_call_channel_name))
            .setDescription(context.getString(R.string.voice_call_channel_description))
            // A ringing/beeping ongoing-call notification on every state
            // update would be its own kind of harassment; LOW importance
            // shows it without a sound per update.
            .setShowBadge(false)
            .build()
        manager.createNotificationChannel(channel)
    }

    @Suppress("InlinedApi") // Manifest.permission.POST_NOTIFICATIONS: safe pre-33 (ContextCompat.checkSelfPermission is version-safe for it).
    override fun show(state: CallNotificationState) {
        // Inlined at the call site, not factored into a helper: Lint's
        // MissingPermission dataflow check only recognizes the guard when it
        // wraps the permission-gated call directly.
        if (ContextCompat.checkSelfPermission(
                context,
                Manifest.permission.POST_NOTIFICATIONS,
            ) != PackageManager.PERMISSION_GRANTED
        ) {
            return
        }
        manager.notify(NOTIFICATION_ID, build(state))
    }

    override fun clear() {
        manager.cancel(NOTIFICATION_ID)
    }

    /** The initial notification a mic-type FGS must present to [android.app.Service.startForeground] (docs/03 §3.3). */
    public fun initial(): android.app.Notification =
        build(CallNotificationState(phase = CallNotificationPhase.CONNECTING, muted = false))

    private fun build(state: CallNotificationState): android.app.Notification {
        val contentText = context.getString(
            when (state.phase) {
                CallNotificationPhase.CONNECTING -> R.string.voice_call_status_connecting
                CallNotificationPhase.IN_CALL -> R.string.voice_call_status_in_call
                CallNotificationPhase.RECONNECTING -> R.string.voice_call_status_reconnecting
            },
        )
        val muteLabel = context.getString(
            if (state.muted) R.string.voice_call_action_unmute else R.string.voice_call_action_mute,
        )

        return NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_call_notification)
            .setContentTitle(context.getString(R.string.voice_call_notification_title))
            .setContentText(contentText)
            .setContentIntent(contentIntent())
            // Non-dismissable: docs/03 §3.3's "ongoing, non-dismissable call
            // notification" — the user ends the call from its own action or
            // the in-app overlay, never by swiping the notification away.
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setCategory(NotificationCompat.CATEGORY_CALL)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .addAction(0, muteLabel, actionIntent(VoiceCallService.ACTION_MUTE))
            .addAction(0, context.getString(R.string.voice_call_action_end), actionIntent(VoiceCallService.ACTION_END))
            .build()
    }

    private fun contentIntent(): PendingIntent? {
        val launchIntent = context.packageManager.getLaunchIntentForPackage(context.packageName)
            ?: return null
        launchIntent.flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
        return PendingIntent.getActivity(
            context,
            REQUEST_CODE_CONTENT,
            launchIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
    }

    private fun actionIntent(action: String): PendingIntent {
        val intent = Intent(context, VoiceCallService::class.java).setAction(action)
        return PendingIntent.getService(
            context,
            action.hashCode(),
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
    }

    public companion object {
        public const val CHANNEL_ID: String = "voice_call"
        public const val NOTIFICATION_ID: Int = 4201
        private const val REQUEST_CODE_CONTENT: Int = 0
    }
}
