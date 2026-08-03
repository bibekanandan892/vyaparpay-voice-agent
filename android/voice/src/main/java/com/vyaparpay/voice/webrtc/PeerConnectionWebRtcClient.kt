package com.vyaparpay.voice.webrtc

import com.vyaparpay.core.network.IceServerDto
import java.nio.ByteBuffer
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException
import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.suspendCancellableCoroutine
import org.webrtc.AudioSource
import org.webrtc.AudioTrack
import org.webrtc.DataChannel
import org.webrtc.IceCandidate
import org.webrtc.MediaConstraints
import org.webrtc.MediaStream
import org.webrtc.PeerConnection
import org.webrtc.PeerConnectionFactory
import org.webrtc.RtpReceiver
import org.webrtc.RtpSender
import org.webrtc.RtpTransceiver
import org.webrtc.SdpObserver
import org.webrtc.SessionDescription

/**
 * The only class in the app that drives org.webrtc (docs/03 §3.4) — libwebrtc
 * via the maintained `io.github.webrtc-sdk:android` artifact (canon ADR-1).
 * Constructed exclusively by [WebRtcClientFactory], which is where the native
 * library is loaded; nothing else may instantiate it, so JVM unit tests never
 * touch a `.so`.
 *
 * Judgment calls, flagged per house style:
 *
 * 1. **The `ctx` channel is created before the offer** so the SCTP m-line is
 *    negotiated in the initial SDP round trip, not a renegotiation
 *    (docs/03 §3.4). Order in [start] is load-bearing.
 * 2. **Mono ~24 kbps is set as a sender bitrate cap, not SDP munging**
 *    (docs/06 §3.4: "we do not hand-munge SDP"). Opus DTX/FEC ride the
 *    libwebrtc defaults the doc already assumes.
 * 3. **Events carry a replay buffer**: candidate gathering starts during
 *    `setLocalDescription`, inside [start], before any collector can attach.
 *    Replay makes trickle-ICE unlosable rather than carefully ordered.
 * 4. **`onConnectionChange` is folded to the doc's event vocabulary**: first
 *    CONNECTED → [RtcEvent.PeerConnected]; a later CONNECTED →
 *    [RtcEvent.TransportResumed]; DISCONNECTED/FAILED →
 *    [RtcEvent.TransportLost]. The `Signaling`/`Connecting` split the state
 *    machine needs (docs/03 §3.2) falls out of *when* these fire, not of
 *    extra event types.
 * 5. **Disposal follows the reference ordering** (channel → peer → source;
 *    tracks die with the peer's senders): double-disposing a track through
 *    both the sender and an explicit `dispose()` is a native crash, not an
 *    exception.
 * 6. **A remote `null` candidate is a logged no-op** — the end-of-candidates
 *    marker (docs/13 §6); libwebrtc, like aiortc, needs no explicit signal.
 *
 * Threading: libwebrtc delivers observer callbacks on its internal signaling
 * thread. They are re-emitted onto [events]/[incoming] via `tryEmit` (never
 * blocking that thread) and consumed on the call scope — no libwebrtc thread
 * ever touches app state directly (docs/03 §3.4).
 */
internal class PeerConnectionWebRtcClient(
    private val factory: PeerConnectionFactory,
    /** Disposes factory-scoped resources (factory itself, audio device module). */
    private val disposeShared: () -> Unit,
) : WebRtcClient {

    private val _events = MutableSharedFlow<RtcEvent>(
        replay = EVENT_REPLAY,
        extraBufferCapacity = EVENT_BUFFER,
        onBufferOverflow = BufferOverflow.DROP_OLDEST,
    )
    override val events: SharedFlow<RtcEvent> = _events.asSharedFlow()

    private val _incoming = MutableSharedFlow<ByteArray>(
        extraBufferCapacity = INCOMING_BUFFER,
        onBufferOverflow = BufferOverflow.DROP_OLDEST,
    )
    override val incoming: Flow<ByteArray> = _incoming.asSharedFlow()

    private val closed = AtomicBoolean(false)
    private val everConnected = AtomicBoolean(false)

    @Volatile private var peer: PeerConnection? = null
    @Volatile private var dataChannel: DataChannel? = null
    @Volatile private var audioSource: AudioSource? = null
    @Volatile private var localAudioTrack: AudioTrack? = null

    override suspend fun start(iceServers: List<IceServerDto>): Sdp {
        check(peer == null) { "start() is one-shot: one PeerConnection per call (docs/03 §2.2)" }
        check(!closed.get()) { "start() after close()" }

        val configuration = PeerConnection.RTCConfiguration(
            iceServers.map(::toRtcIceServer),
        ).apply {
            sdpSemantics = PeerConnection.SdpSemantics.UNIFIED_PLAN
            // Keep gathering across the call so an ICE restart on network
            // change has fresh candidates to trickle (docs/03 §3.4).
            continualGatheringPolicy =
                PeerConnection.ContinualGatheringPolicy.GATHER_CONTINUALLY
        }
        val connection = factory.createPeerConnection(configuration, PeerObserver())
            ?: throw WebRtcException("PeerConnection creation failed")
        peer = connection

        // Judgment call 1: channel before offer.
        val channel = connection.createDataChannel(CONTEXT_CHANNEL_LABEL, DataChannel.Init())
            ?: throw WebRtcException("'$CONTEXT_CHANNEL_LABEL' data channel creation failed")
        channel.registerObserver(ContextChannelObserver())
        dataChannel = channel

        // The client-side half of the audio-processing contract (docs/06 §2.3):
        // echo cancellation, noise suppression, auto gain, high-pass all on.
        val source = factory.createAudioSource(micConstraints())
        audioSource = source
        val track = factory.createAudioTrack(MIC_TRACK_ID, source)
        localAudioTrack = track
        val sender = connection.addTrack(track, listOf(MEDIA_STREAM_ID))
        capAudioBitrate(sender)

        val offer = awaitCreateDescription("createOffer") { observer ->
            connection.createOffer(observer, MediaConstraints())
        }
        awaitSetDescription("setLocalDescription") { observer ->
            connection.setLocalDescription(observer, offer)
        }
        return Sdp(offer.description)
    }

    override suspend fun applyAnswer(sdp: Sdp) {
        val connection = peer ?: throw WebRtcException("applyAnswer() before start()")
        awaitSetDescription("setRemoteDescription") { observer ->
            connection.setRemoteDescription(
                observer,
                SessionDescription(SessionDescription.Type.ANSWER, sdp.value),
            )
        }
    }

    override fun addRemoteCandidate(candidate: RemoteIceCandidate) {
        // Teardown races are routine — a candidate landing after close is noise.
        val connection = peer ?: return
        // Judgment call 6: null candidate = end-of-candidates marker, no-op.
        val sdp = candidate.candidate ?: return
        connection.addIceCandidate(
            IceCandidate(candidate.sdpMid, candidate.sdpMLineIndex ?: 0, sdp),
        )
    }

    override suspend fun restartIce(): Sdp {
        val connection = peer ?: throw WebRtcException("restartIce() before start()")
        connection.restartIce()
        val offer = awaitCreateDescription("createOffer(iceRestart)") { observer ->
            connection.createOffer(observer, MediaConstraints())
        }
        awaitSetDescription("setLocalDescription(iceRestart)") { observer ->
            connection.setLocalDescription(observer, offer)
        }
        return Sdp(offer.description)
    }

    override fun setMuted(muted: Boolean) {
        localAudioTrack?.setEnabled(!muted)
    }

    override suspend fun sendContext(bytes: ByteArray) {
        val channel = dataChannel
            ?: throw WebRtcException("sendContext() before start()")
        if (channel.state() != DataChannel.State.OPEN) {
            // Fail loud, exactly like the server's send_data_channel: the
            // caller owns lifecycle and must not silently drop context.
            throw WebRtcException("'$CONTEXT_CHANNEL_LABEL' data channel is not open")
        }
        // binary = false: the ctx protocol is JSON text (docs/13 §4).
        channel.send(DataChannel.Buffer(ByteBuffer.wrap(bytes), false))
    }

    override suspend fun close() {
        if (!closed.compareAndSet(false, true)) return
        // Judgment call 5: channel first, then peer (which owns the senders
        // and their tracks), then the source, then factory-scoped resources.
        dataChannel?.let { channel ->
            channel.unregisterObserver()
            channel.close()
            channel.dispose()
        }
        dataChannel = null
        localAudioTrack = null
        peer?.let { connection ->
            connection.close()
            connection.dispose()
        }
        peer = null
        audioSource?.dispose()
        audioSource = null
        _events.tryEmit(RtcEvent.Closed)
        disposeShared()
    }

    // ------------------------------------------------------------------
    // org.webrtc plumbing
    // ------------------------------------------------------------------

    private fun toRtcIceServer(dto: IceServerDto): PeerConnection.IceServer {
        val builder = PeerConnection.IceServer.builder(dto.urls)
        dto.username?.let(builder::setUsername)
        dto.credential?.let(builder::setPassword)
        return builder.createIceServer()
    }

    private fun micConstraints(): MediaConstraints = MediaConstraints().apply {
        mandatory.add(MediaConstraints.KeyValuePair("googEchoCancellation", "true"))
        mandatory.add(MediaConstraints.KeyValuePair("googNoiseSuppression", "true"))
        mandatory.add(MediaConstraints.KeyValuePair("googAutoGainControl", "true"))
        mandatory.add(MediaConstraints.KeyValuePair("googHighpassFilter", "true"))
    }

    /** Judgment call 2: ~24 kbps target via sender parameters (docs/06 §3.1). */
    private fun capAudioBitrate(sender: RtpSender) {
        val parameters = sender.parameters
        if (parameters.encodings.isNullOrEmpty()) return
        parameters.encodings.forEach { it.maxBitrateBps = TARGET_AUDIO_BITRATE_BPS }
        sender.parameters = parameters
    }

    private suspend fun awaitCreateDescription(
        operation: String,
        run: (SdpObserver) -> Unit,
    ): SessionDescription = suspendCancellableCoroutine { continuation ->
        run(object : SdpObserver {
            override fun onCreateSuccess(description: SessionDescription?) {
                if (description == null) {
                    continuation.resumeWithException(
                        WebRtcException("$operation produced no description"),
                    )
                } else {
                    continuation.resume(description)
                }
            }

            override fun onCreateFailure(error: String?) {
                continuation.resumeWithException(WebRtcException("$operation failed: $error"))
            }

            override fun onSetSuccess() = Unit
            override fun onSetFailure(error: String?) = Unit
        })
    }

    private suspend fun awaitSetDescription(
        operation: String,
        run: (SdpObserver) -> Unit,
    ): Unit = suspendCancellableCoroutine { continuation ->
        run(object : SdpObserver {
            override fun onSetSuccess() {
                continuation.resume(Unit)
            }

            override fun onSetFailure(error: String?) {
                continuation.resumeWithException(WebRtcException("$operation failed: $error"))
            }

            override fun onCreateSuccess(description: SessionDescription?) = Unit
            override fun onCreateFailure(error: String?) = Unit
        })
    }

    private inner class PeerObserver : PeerConnection.Observer {

        override fun onIceCandidate(candidate: IceCandidate?) {
            candidate ?: return
            _events.tryEmit(
                RtcEvent.IceCandidateFound(
                    LocalIceCandidate(candidate.sdp, candidate.sdpMid, candidate.sdpMLineIndex),
                ),
            )
        }

        override fun onConnectionChange(newState: PeerConnection.PeerConnectionState?) {
            // Judgment call 4.
            when (newState) {
                PeerConnection.PeerConnectionState.CONNECTED -> _events.tryEmit(
                    if (everConnected.compareAndSet(false, true)) RtcEvent.PeerConnected
                    else RtcEvent.TransportResumed,
                )
                PeerConnection.PeerConnectionState.DISCONNECTED,
                PeerConnection.PeerConnectionState.FAILED,
                -> _events.tryEmit(RtcEvent.TransportLost)
                PeerConnection.PeerConnectionState.CLOSED ->
                    if (!closed.get()) _events.tryEmit(RtcEvent.Closed)
                else -> Unit
            }
        }

        override fun onTrack(transceiver: RtpTransceiver?) {
            // Remote audio playout: an enabled remote AudioTrack renders
            // through the audio device module automatically — no sink to wire.
            (transceiver?.receiver?.track() as? AudioTrack)?.setEnabled(true)
        }

        override fun onDataChannel(channel: DataChannel?) {
            // The one `ctx` channel is client-opened (docs/13 §4); a
            // server-opened channel is unexpected — ignore, mirroring the
            // server's extra-channel handling.
        }

        override fun onSignalingChange(newState: PeerConnection.SignalingState?) = Unit
        override fun onIceConnectionChange(newState: PeerConnection.IceConnectionState?) = Unit
        override fun onIceConnectionReceivingChange(receiving: Boolean) = Unit
        override fun onIceGatheringChange(newState: PeerConnection.IceGatheringState?) = Unit
        override fun onIceCandidatesRemoved(candidates: Array<out IceCandidate>?) = Unit
        override fun onAddStream(stream: MediaStream?) = Unit
        override fun onRemoveStream(stream: MediaStream?) = Unit
        override fun onAddTrack(receiver: RtpReceiver?, streams: Array<out MediaStream>?) = Unit
        override fun onRenegotiationNeeded() = Unit
    }

    private inner class ContextChannelObserver : DataChannel.Observer {

        override fun onMessage(buffer: DataChannel.Buffer?) {
            buffer ?: return
            val bytes = ByteArray(buffer.data.remaining())
            buffer.data.get(bytes)
            _incoming.tryEmit(bytes)
        }

        override fun onBufferedAmountChange(previousAmount: Long) = Unit
        override fun onStateChange() = Unit
    }

    internal companion object {
        /** docs/13 §4: the one data channel, label `ctx`, reliable + ordered defaults. */
        internal const val CONTEXT_CHANNEL_LABEL: String = "ctx"

        private const val MIC_TRACK_ID: String = "vyaparpay-mic0"
        private const val MEDIA_STREAM_ID: String = "vyaparpay"

        /** docs/06 §3.1: ~24 kbps mono Opus target. */
        private const val TARGET_AUDIO_BITRATE_BPS: Int = 24_000

        private const val EVENT_REPLAY: Int = 32
        private const val EVENT_BUFFER: Int = 32
        private const val INCOMING_BUFFER: Int = 256
    }
}
