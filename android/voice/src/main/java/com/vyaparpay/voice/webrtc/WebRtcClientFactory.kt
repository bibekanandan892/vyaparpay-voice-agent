package com.vyaparpay.voice.webrtc

import android.content.Context
import java.util.concurrent.atomic.AtomicBoolean
import org.webrtc.PeerConnectionFactory
import org.webrtc.audio.JavaAudioDeviceModule

/**
 * The single place the native libwebrtc library is loaded and a
 * [WebRtcClient] is born (docs/03 §2.2: "the `PeerConnectionFactory`
 * bootstrap is a cheap once-per-process static, but the `PeerConnection` it
 * creates lives exactly one call").
 *
 * Confining construction here is what makes the module JVM-testable: every
 * other class — controller, state machine, signaling — depends on the
 * [WebRtcClient] *interface*, and only a caller that goes through this
 * factory (in practice `VoiceCallService`, on a real device) ever triggers
 * `System.loadLibrary("jingle_peerconnection_so")`.
 *
 * The audio device module carries the docs/06 §2.3 contract: hardware AEC and
 * NS where the handset has the DSP, libwebrtc's software AEC3/NS as the floor
 * where it does not. The `VOICE_COMMUNICATION` capture preset and
 * `MODE_IN_COMMUNICATION` — the other half of that contract — are audio-session
 * state owned by the `VoiceCallService` task.
 */
public object WebRtcClientFactory {

    private val nativeInitialized = AtomicBoolean(false)

    /**
     * Build a call-scoped [WebRtcClient]. The returned client owns the
     * factory and audio device module created here and releases both in
     * [WebRtcClient.close] — create one per call, close it when the call
     * ends (docs/03 §2.2's service-held scoping).
     */
    public fun create(context: Context): WebRtcClient {
        val appContext = context.applicationContext
        ensureNativeInitialized(appContext)
        val audioDeviceModule = JavaAudioDeviceModule.builder(appContext)
            .setUseHardwareAcousticEchoCanceler(true)
            .setUseHardwareNoiseSuppressor(true)
            .createAudioDeviceModule()
        val factory = PeerConnectionFactory.builder()
            .setAudioDeviceModule(audioDeviceModule)
            .createPeerConnectionFactory()
        return PeerConnectionWebRtcClient(factory) {
            factory.dispose()
            audioDeviceModule.release()
        }
    }

    private fun ensureNativeInitialized(appContext: Context) {
        if (!nativeInitialized.compareAndSet(false, true)) return
        PeerConnectionFactory.initialize(
            PeerConnectionFactory.InitializationOptions.builder(appContext)
                .createInitializationOptions(),
        )
    }
}
