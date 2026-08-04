package com.vyaparpay.voice.audio

import kotlinx.coroutines.flow.SharedFlow

/**
 * The Android audio session that wraps a call (docs/03 §3.4's "audio focus and
 * routing").
 *
 * This is an interface for the same reason [com.vyaparpay.voice.webrtc.WebRtcClient]
 * and [com.vyaparpay.voice.signaling.SignalingClient] are: the *policy* — when
 * focus is taken, when it is given back, what a transient loss means — is the
 * part worth testing, and `AudioManager` is final, framework-owned, and absent
 * from a JVM test JVM. [AndroidCallAudioSession] is the only implementation
 * that touches `android.media`, so [com.vyaparpay.voice.service.VoiceCallCoordinator]
 * and its tests run on a plain JVM with a fake.
 *
 * Ownership note: `WebRtcClient`'s KDoc explicitly hands this surface to the
 * `VoiceCallService` task, and [com.vyaparpay.voice.webrtc.WebRtcClientFactory]
 * says the same of `MODE_IN_COMMUNICATION`. The peer owns the *media*; this
 * owns the *device*.
 */
public interface CallAudioSession {

    /**
     * Focus transitions reported by the platform, normalized to the four cases
     * a voice call actually distinguishes.
     *
     * Not replay-buffered: a focus event is a fact about *now*, and replaying a
     * stale loss onto a later call would mute a live mic for no reason. The
     * coordinator subscribes before it calls [acquire], so nothing is missed.
     */
    public val focusChanges: SharedFlow<AudioFocusChange>

    /**
     * Take the device for a voice call: `MODE_IN_COMMUNICATION` (which engages
     * the handset's hardware AEC via the `VOICE_COMMUNICATION` capture preset —
     * docs/06 §3.3's "the other half of that contract") and transient audio
     * focus over `USAGE_VOICE_COMMUNICATION` / `CONTENT_TYPE_SPEECH`
     * (docs/03 §3.4).
     *
     * Must be called **before** the mic track goes live. Returns whether focus
     * was actually granted; a refusal is reported, not fatal — see
     * [com.vyaparpay.voice.service.VoiceCallCoordinator] for why a support call
     * proceeds anyway. Idempotent: a second call while held is a no-op that
     * re-reports the first result.
     */
    public fun acquire(): Boolean

    /**
     * Abandon focus and put the device back exactly as it was found — mode
     * restored, communication device cleared. **Idempotent**, because every
     * terminal path converges here and the service's `onDestroy` runs it again
     * defensively; abandoning focus twice must not abandon a *later* call's
     * focus.
     */
    public fun release()

    /**
     * Route the call's audio. Earpiece is the default and the one the product
     * nudges toward: it has the best AEC and is the least likely source of
     * false barge-in in a loud shop (docs/06 §6.5).
     */
    public fun setRoute(route: AudioRoute)
}

/**
 * A platform focus transition, reduced to what a voice call does about it.
 *
 * `AUDIOFOCUS_LOSS_TRANSIENT` and `AUDIOFOCUS_LOSS_TRANSIENT_CAN_DUCK` are
 * deliberately kept apart rather than folded together: a voice call must not
 * duck (halving the caller's volume mid-sentence is worse than doing nothing),
 * so the two platform signals lead to genuinely different behaviour.
 */
public enum class AudioFocusChange {

    /** Focus came back after a transient loss — resume the uplink. */
    GAINED,

    /** Something took the audio path briefly — typically an incoming cellular call. */
    LOST_TRANSIENT,

    /** Focus is gone for good; another app owns the audio path now. */
    LOST,

    /** The platform asked us to duck. A speech call declines (see the enum KDoc). */
    DUCK_REQUESTED,
}

/**
 * Where the call's audio plays (docs/03 §3.4's `Earpiece | Speaker | Bluetooth`).
 */
public enum class AudioRoute {

    /** The default: best AEC, least false barge-in (docs/06 §6.5). */
    EARPIECE,

    /** Speakerphone — the most likely source of false barge-in on a weak-AEC handset. */
    SPEAKER,

    /**
     * A Bluetooth headset over SCO. Honest limitation (docs/03 §3.4): below
     * API 31 this goes through the legacy `startBluetoothSco()` path, which is
     * known-flaky on cheap handsets.
     */
    BLUETOOTH,
}
