package com.vyaparpay.voice.service

import com.vyaparpay.voice.CallState
import com.vyaparpay.voice.audio.AudioFocusChange
import com.vyaparpay.voice.audio.CallAudioSession
import com.vyaparpay.voice.notification.CallNotificationPhase
import com.vyaparpay.voice.notification.CallNotificationState
import com.vyaparpay.voice.notification.CallNotifier
import com.vyaparpay.voice.requiresForegroundService
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch

/**
 * `VoiceCallService`'s policy brain (docs/03 §3.3): watches the call's
 * [CallState], and decides when the service must be in the foreground, when
 * audio focus is taken and given back, what the notification says, and when
 * the service must stop.
 *
 * Deliberately framework-free — no `android.*` import — for the same reason
 * [com.vyaparpay.voice.CallController] is: [CallAudioSession] and
 * [CallNotifier] are interfaces, so this whole policy runs on a plain JVM
 * against fakes, with the actual `AudioManager`/`NotificationManager` glue
 * confined to [com.vyaparpay.voice.audio.AndroidCallAudioSession] and
 * [com.vyaparpay.voice.notification.AndroidCallNotifier].
 *
 * **The one invariant this class exists to guarantee:** the service cannot
 * outlive a terminal [CallState] — a leaked service is a live mic
 * (docs/03 §2.2) — and audio focus is abandoned exactly once, on that same
 * transition, never left dangling for a later call to inherit.
 *
 * Expected wiring: the owning service calls [start] *before* driving any
 * [CallState] transitions (e.g. before `CallController.startCall`), so the
 * first collected value is genuinely the call's first state and the
 * `Idle → Requesting` edge is never missed.
 *
 * **Threading contract.** Every mutation of this class's own state happens on
 * [scope] — [start]'s two collectors *and* [toggleMute] all funnel through it
 * — the same single-serial-execution discipline `CallController` applies to
 * `CallEvent` dispatch (docs/03 §3.2). `VoiceCallService` must supply a
 * [scope] backed by a single confined dispatcher in production, or a mute
 * toggle arriving on the service's main thread could interleave with a
 * focus-change collector mid-decision. A `TestScope`'s virtual-time
 * dispatcher already serializes everything, so tests may pass any scope.
 */
public class VoiceCallCoordinator(
    private val callState: StateFlow<CallState>,
    private val audioSession: CallAudioSession,
    private val notifier: CallNotifier,
    private val scope: CoroutineScope,
    private val setMuted: (Boolean) -> Unit,
    private val hangUp: () -> Unit,
    private val onForegroundServiceRequired: () -> Unit,
    private val onCallEnded: () -> Unit,
) {

    // Not @Volatile: safety comes from confinement to `scope` (see the
    // threading contract above), not from visibility alone — a volatile
    // field would still let two threads interleave a read-modify-write of
    // `userMuted`/`autoMuted` together.
    private var currentPhase: CallNotificationPhase? = null
    private var userMuted: Boolean = false
    private var autoMuted: Boolean = false
    private var everForegrounded: Boolean = false

    /** Begin driving policy off [callState] and [CallAudioSession.focusChanges]. Call once. */
    public fun start() {
        scope.launch { collectCallState() }
        scope.launch { collectFocusChanges() }
    }

    /**
     * The user tapped the notification's mute action (docs/03 §3.3's
     * `ACTION_MUTE`). Tracked here — not just forwarded — so a focus-driven
     * auto-mute and a user's own mute never clobber each other: the uplink
     * stays muted while *either* is true, and gaining focus back only lifts
     * the half this class imposed.
     *
     * Dispatched onto [scope] rather than mutating inline: the caller may be
     * on the service's main thread, and every read of `userMuted`/`autoMuted`
     * must happen on the same confined executor as the collectors in [start]
     * (see the threading contract above).
     */
    public fun toggleMute() {
        scope.launch {
            userMuted = !userMuted
            applyMuteState()
        }
    }

    private suspend fun collectCallState() {
        callState.collect { state ->
            if (state.requiresForegroundService && !everForegrounded) {
                everForegrounded = true
                onForegroundServiceRequired()
                // Focus is acquired as soon as the service is in the
                // foreground — before WebRtcClient.start() opens the mic on
                // the Requesting -> Signaling transition (docs/03 §3.2) — so
                // MODE_IN_COMMUNICATION and the hardware-AEC contract
                // (docs/06 §3.3) are live *before* capture begins, not after.
                // (docs/03 §3.4's "on InCall the client requests focus"
                // predates the audio-session ownership handoff to this task —
                // see CallAudioSession's KDoc.)
                audioSession.acquire()
            }

            currentPhase = state.toNotificationPhase()
            pushNotification()

            if (state is CallState.Ended) {
                // Every terminal path converges here — same rule as
                // CallEffect.ReleaseCall, one layer up: idempotent by
                // construction (AndroidCallAudioSession.release() no-ops if
                // focus was never held), and this branch itself only runs
                // once because a StateFlow never re-emits an equal value and
                // the reducer holds Ended steady once reached.
                notifier.clear()
                audioSession.release()
                onCallEnded()
            }
        }
    }

    private suspend fun collectFocusChanges() {
        audioSession.focusChanges.collect { change ->
            when (change) {
                AudioFocusChange.LOST_TRANSIENT, AudioFocusChange.DUCK_REQUESTED -> {
                    // A voice call declines to duck (CallAudioSession's
                    // AudioFocusChange KDoc); both signals mute the uplink
                    // instead of halving it.
                    autoMuted = true
                    applyMuteState()
                }
                AudioFocusChange.GAINED -> {
                    autoMuted = false
                    applyMuteState()
                }
                AudioFocusChange.LOST ->
                    // Focus is gone for good — another app owns the audio
                    // path now. The docs/03 §7 rule, generalized: end the
                    // call honestly rather than keep a "live" call with no
                    // working audio path.
                    hangUp()
            }
        }
    }

    private fun applyMuteState() {
        setMuted(userMuted || autoMuted)
        pushNotification()
    }

    private fun pushNotification() {
        val phase = currentPhase ?: return
        notifier.show(CallNotificationState(phase = phase, muted = userMuted || autoMuted))
    }
}

/** `Idle`/`Ended` show nothing — the service is not in the foreground at either. */
private fun CallState.toNotificationPhase(): CallNotificationPhase? = when (this) {
    CallState.Requesting, CallState.Signaling, CallState.Connecting -> CallNotificationPhase.CONNECTING
    CallState.InCall -> CallNotificationPhase.IN_CALL
    CallState.Reconnecting -> CallNotificationPhase.RECONNECTING
    CallState.Idle, is CallState.Ended -> null
}
