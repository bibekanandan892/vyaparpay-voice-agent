package com.vyaparpay.voice.audio

import android.media.AudioAttributes
import android.media.AudioDeviceInfo
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.os.Build
import androidx.annotation.RequiresApi
import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.asSharedFlow

/**
 * The one class in `:voice` that touches `android.media` (docs/03 §3.4).
 *
 * Everything here is API-level bookkeeping around three ideas: take transient
 * focus for speech, put the device in `MODE_IN_COMMUNICATION` so the hardware
 * echo canceller engages (docs/06 §3.3), and restore whatever was there before
 * when the call ends. The policy that decides *when* to do those things lives
 * in [com.vyaparpay.voice.service.VoiceCallCoordinator], which is why it — and
 * not this class — carries the unit tests.
 *
 * Takes an [AudioManager] rather than a `Context` so the seam is as thin as the
 * framework allows and the class has exactly one collaborator.
 */
public class AndroidCallAudioSession(
    private val audioManager: AudioManager,
) : CallAudioSession {

    private val _focusChanges = MutableSharedFlow<AudioFocusChange>(
        replay = 0,
        extraBufferCapacity = FOCUS_BUFFER,
        onBufferOverflow = BufferOverflow.DROP_OLDEST,
    )
    override val focusChanges: SharedFlow<AudioFocusChange> = _focusChanges.asSharedFlow()

    private val focusListener = AudioManager.OnAudioFocusChangeListener { change ->
        val mapped = when (change) {
            AudioManager.AUDIOFOCUS_GAIN -> AudioFocusChange.GAINED
            AudioManager.AUDIOFOCUS_LOSS_TRANSIENT -> AudioFocusChange.LOST_TRANSIENT
            AudioManager.AUDIOFOCUS_LOSS_TRANSIENT_CAN_DUCK -> AudioFocusChange.DUCK_REQUESTED
            AudioManager.AUDIOFOCUS_LOSS -> AudioFocusChange.LOST
            // Forward-compatibility, the same rule the wire protocols follow
            // (docs/13 §9): an unknown focus code is ignored, never guessed at.
            else -> null
        }
        if (mapped != null) _focusChanges.tryEmit(mapped)
    }

    /** Non-null exactly while focus is held — the idempotency latch for [release]. */
    private var focusRequest: AudioFocusRequest? = null

    /** True while focus is held on the pre-26 path, where there is no request object. */
    private var legacyFocusHeld: Boolean = false

    private var previousMode: Int = AudioManager.MODE_NORMAL

    @Suppress("DEPRECATION")
    private var previousSpeakerphoneOn: Boolean = false

    private var held: Boolean = false
    private var focusGranted: Boolean = false

    // Synchronized, not just @Volatile-backed: VoiceCallCoordinator confines
    // its own calls to one dispatcher, but VoiceCallService's onDestroy() is
    // a defensive backstop that calls release() directly from the platform's
    // callback thread, racing a coordinator-driven acquire()/release() that
    // may still be in flight. release()'s own idempotency promise ("safe
    // when nothing was ever opened") only holds under concurrent callers if
    // the read-then-mutate of `held` itself cannot interleave.
    @Synchronized
    override fun acquire(): Boolean {
        if (held) return focusGranted
        held = true

        previousMode = audioManager.mode
        @Suppress("DEPRECATION")
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) {
            previousSpeakerphoneOn = audioManager.isSpeakerphoneOn
        }

        focusGranted = requestFocus()

        // Set the mode whether or not focus was granted. The mode is what makes
        // the capture path use the VOICE_COMMUNICATION preset, and a call that
        // is going to happen anyway (see VoiceCallCoordinator) is far better off
        // with hardware AEC than without it.
        audioManager.mode = AudioManager.MODE_IN_COMMUNICATION

        // docs/06 §6.5: earpiece is the default, and the product nudges toward
        // it because speakerphone on a weak-AEC handset is the field's main
        // source of false barge-in.
        setRoute(AudioRoute.EARPIECE)

        return focusGranted
    }

    @Synchronized
    override fun release() {
        if (!held) return
        held = false
        focusGranted = false

        abandonFocus()
        clearRoute()
        audioManager.mode = previousMode
    }

    @Synchronized
    override fun setRoute(route: AudioRoute) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            setRouteModern(route)
        } else {
            setRouteLegacy(route)
        }
    }

    // ------------------------------------------------------------------
    // Focus
    // ------------------------------------------------------------------

    private fun requestFocus(): Boolean {
        // docs/03 §3.4 pins the shape: AUDIOFOCUS_GAIN_TRANSIENT over
        // USAGE_VOICE_COMMUNICATION / CONTENT_TYPE_SPEECH. Transient rather
        // than permanent because a support call is an interruption of whatever
        // the user was listening to, not a replacement for it — the music app
        // should come back when Asha hangs up.
        val attributes = AudioAttributes.Builder()
            .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
            .build()

        val result = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val request = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT)
                .setAudioAttributes(attributes)
                .setOnAudioFocusChangeListener(focusListener)
                // A speech call declines to duck; it asks to be paused instead,
                // and the coordinator handles the resulting transient loss by
                // muting the uplink rather than halving the volume.
                .setWillPauseWhenDucked(true)
                .build()
            focusRequest = request
            audioManager.requestAudioFocus(request)
        } else {
            legacyFocusHeld = true
            @Suppress("DEPRECATION")
            audioManager.requestAudioFocus(
                focusListener,
                AudioManager.STREAM_VOICE_CALL,
                AudioManager.AUDIOFOCUS_GAIN_TRANSIENT,
            )
        }
        return result == AudioManager.AUDIOFOCUS_REQUEST_GRANTED
    }

    private fun abandonFocus() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            focusRequest?.let { audioManager.abandonAudioFocusRequest(it) }
            focusRequest = null
        } else if (legacyFocusHeld) {
            legacyFocusHeld = false
            @Suppress("DEPRECATION")
            audioManager.abandonAudioFocus(focusListener)
        }
    }

    // ------------------------------------------------------------------
    // Routing
    // ------------------------------------------------------------------

    @RequiresApi(Build.VERSION_CODES.S)
    private fun setRouteModern(route: AudioRoute) {
        val wanted = when (route) {
            AudioRoute.EARPIECE -> AudioDeviceInfo.TYPE_BUILTIN_EARPIECE
            AudioRoute.SPEAKER -> AudioDeviceInfo.TYPE_BUILTIN_SPEAKER
            AudioRoute.BLUETOOTH -> AudioDeviceInfo.TYPE_BLUETOOTH_SCO
        }
        // availableCommunicationDevices is the authority — a tablet may have no
        // earpiece at all, and asking for one that is not there throws.
        val device = audioManager.availableCommunicationDevices.firstOrNull { it.type == wanted }
        if (device != null) audioManager.setCommunicationDevice(device)
    }

    @Suppress("DEPRECATION")
    private fun setRouteLegacy(route: AudioRoute) {
        when (route) {
            AudioRoute.EARPIECE -> {
                audioManager.stopBluetoothSco()
                audioManager.isBluetoothScoOn = false
                audioManager.isSpeakerphoneOn = false
            }
            AudioRoute.SPEAKER -> {
                audioManager.stopBluetoothSco()
                audioManager.isBluetoothScoOn = false
                audioManager.isSpeakerphoneOn = true
            }
            AudioRoute.BLUETOOTH -> {
                audioManager.isSpeakerphoneOn = false
                // docs/03 §3.4 calls this path out as known-flaky on cheap
                // handsets; it is retained only because minSdk is 24.
                audioManager.startBluetoothSco()
                audioManager.isBluetoothScoOn = true
            }
        }
    }

    private fun clearRoute() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            audioManager.clearCommunicationDevice()
        } else {
            @Suppress("DEPRECATION")
            audioManager.stopBluetoothSco()
            @Suppress("DEPRECATION")
            audioManager.isBluetoothScoOn = false
            @Suppress("DEPRECATION")
            audioManager.isSpeakerphoneOn = previousSpeakerphoneOn
        }
    }

    private companion object {
        /**
         * Focus events are rare and the collector is always attached, but a
         * non-zero buffer means `tryEmit` from the platform's listener thread
         * never silently drops one because the collector had not yet resumed.
         */
        private const val FOCUS_BUFFER: Int = 8
    }
}
