package com.vyaparpay.voice

import com.vyaparpay.core.network.ApiError
import com.vyaparpay.core.network.ApiResult
import com.vyaparpay.core.network.ConnectBundleDto
import com.vyaparpay.core.network.SessionCreateRequestDto
import com.vyaparpay.core.network.VyaparApi
import com.vyaparpay.voice.signaling.OutboundSignal
import com.vyaparpay.voice.signaling.SignalFrame
import com.vyaparpay.voice.signaling.SignalingClient
import com.vyaparpay.voice.signaling.SignalingTarget
import com.vyaparpay.voice.webrtc.RemoteIceCandidate
import com.vyaparpay.voice.webrtc.RtcEvent
import com.vyaparpay.voice.webrtc.Sdp
import com.vyaparpay.voice.webrtc.WebRtcClient
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch

/**
 * Wires the pure [CallStateMachine] to the real world: it executes the
 * effects each transition returns, and funnels everything that happens back
 * in — the session API, [SignalingClient] frames, [WebRtcClient] events, UI
 * intents — as [CallEvent]s through **one** serial channel, so the reducer is
 * never re-entered concurrently (docs/03 §3.2's threading rule: libwebrtc's
 * signaling thread, OkHttp's reader thread, and main all converge here).
 *
 * It is deliberately service-free: no Android imports, constructor-injected
 * collaborators behind interfaces, all timing via `delay` on the injected
 * scope — which is exactly what makes the full happy path and every failure
 * branch runnable under a virtual-time test dispatcher. `VoiceCallService`
 * (its own task) will own an instance, hand it a call-scoped
 * `CoroutineScope`, and translate its [state] into notification/FGS
 * lifecycle.
 *
 * Sequence owned here (docs/03 §3.2, docs/13 §6.1): mint session → connect
 * WS → start peer (mic + `ctx` channel) → send offer → trickle local ICE out
 * / apply remote ICE in (tolerating the server's answer-embedded candidates
 * and its `candidate: null` marker) → apply answer → media connects → in
 * call → bye/teardown.
 */
public class CallController(
    private val api: VyaparApi,
    private val signaling: SignalingClient,
    private val webRtc: WebRtcClient,
    private val scope: CoroutineScope,
    private val answerTimeoutMillis: Long = ANSWER_TIMEOUT_MILLIS,
    private val reconnectGraceMillis: Long = RECONNECT_GRACE_MILLIS,
) {

    private val machine = CallStateMachine()

    /** The call's single source of truth, for the overlay and the service. */
    public val state: StateFlow<CallState> = machine.state

    private val events = Channel<CallEvent>(Channel.UNLIMITED)

    @Volatile private var pendingRequest: SessionCreateRequestDto? = null
    @Volatile private var sessionId: String? = null
    private var transportJob: Job? = null
    private var answerTimeoutJob: Job? = null
    private var graceJob: Job? = null

    init {
        scope.launch {
            for (event in events) {
                machine.dispatch(event).forEach { execute(it) }
            }
        }
    }

    /**
     * Begin a call attempt with the session-create body the caller built
     * from `AppStateManager` (docs/13 §2.1). A no-op unless the call is
     * [CallState.Idle].
     */
    public fun startCall(request: SessionCreateRequestDto) {
        pendingRequest = request
        dispatch(CallEvent.SupportTapped)
    }

    /** The user tapped End — valid in any non-terminal state. */
    public fun hangUp() {
        dispatch(CallEvent.UserHungUp)
    }

    /** Track-level mute passthrough (docs/03 §3.4). */
    public fun setMuted(muted: Boolean) {
        webRtc.setMuted(muted)
    }

    private fun dispatch(event: CallEvent) {
        // UNLIMITED channel: trySend cannot fail while the scope lives, and
        // after teardown a dropped stray event is exactly what we want.
        events.trySend(event)
    }

    // ------------------------------------------------------------------
    // Effect execution (runs on the single dispatch loop)
    // ------------------------------------------------------------------

    private fun execute(effect: CallEffect) {
        when (effect) {
            CallEffect.MintSession -> mintSession()
            is CallEffect.OpenTransport -> openTransport(effect.bundle)
            is CallEffect.SendBye -> signaling.send(OutboundSignal.Bye(effect.reason))
            CallEffect.StartReconnectGrace -> startGraceTimer()
            CallEffect.CancelReconnectGrace -> {
                graceJob?.cancel()
                graceJob = null
            }
            CallEffect.EndSessionOnApi -> endSessionOnApi()
            CallEffect.ReleaseCall -> releaseCall()
        }
    }

    private fun mintSession() {
        val request = pendingRequest ?: return
        transportJob = scope.launch {
            when (val result = api.createSession(request)) {
                is ApiResult.Success -> {
                    sessionId = result.data.sessionId
                    dispatch(CallEvent.SessionMinted(result.data))
                }
                is ApiResult.Failure -> dispatch(CallEvent.Failed(result.code))
            }
        }
    }

    private fun openTransport(bundle: ConnectBundleDto) {
        // Replaces the mint job (already completed — it delivered the bundle).
        transportJob = scope.launch {
            launch { collectSignalingFrames() }
            launch { collectRtcEvents() }
            try {
                signaling.connect(
                    SignalingTarget(
                        signalingUrl = bundle.signalingUrl,
                        sessionId = bundle.sessionId,
                        token = bundle.signalingToken,
                    ),
                )
                val offer = webRtc.start(bundle.iceServers)
                signaling.send(OutboundSignal.Offer(offer.value))
                startAnswerTimeout()
            } catch (e: CancellationException) {
                throw e
            } catch (_: Exception) {
                // Connect refused or peer bootstrap failed — either way the
                // control plane never came up (docs/03 §3.2's Signaling
                // failure). The cause was a transport/native error, not a
                // wire code, so there is nothing better than UNKNOWN to map.
                dispatch(CallEvent.Failed(ApiError.UNKNOWN))
            }
        }
    }

    private suspend fun collectSignalingFrames() {
        signaling.frames.collect { frame ->
            when (frame) {
                is SignalFrame.Answer -> {
                    answerTimeoutJob?.cancel()
                    answerTimeoutJob = null
                    try {
                        webRtc.applyAnswer(Sdp(frame.sdp))
                        dispatch(CallEvent.AnswerApplied)
                    } catch (e: CancellationException) {
                        throw e
                    } catch (_: Exception) {
                        dispatch(CallEvent.Failed(ApiError.UNKNOWN))
                    }
                }
                is SignalFrame.RemoteIce -> webRtc.addRemoteCandidate(
                    // candidate == null is the end-of-candidates marker —
                    // forwarded, and a no-op inside the client (docs/13 §6).
                    RemoteIceCandidate(frame.candidate, frame.sdpMid, frame.sdpMLineIndex),
                )
                is SignalFrame.Bye -> dispatch(CallEvent.RemoteBye(frame.reason))
                is SignalFrame.ServerError ->
                    // Same code vocabulary as REST (docs/13 §6); unknown
                    // members fold to UNKNOWN (§9). The reducer decides
                    // whether it is fatal for the current phase.
                    dispatch(CallEvent.Failed(ApiError.fromWireCode(frame.code)))
                is SignalFrame.Closed -> dispatch(CallEvent.SignalingClosed)
            }
        }
    }

    private suspend fun collectRtcEvents() {
        webRtc.events.collect { event ->
            when (event) {
                is RtcEvent.IceCandidateFound -> signaling.send(
                    // Trickle out immediately — media starts on the first
                    // working pair (docs/06 §2).
                    OutboundSignal.LocalIce(
                        candidate = event.candidate.candidate,
                        sdpMid = event.candidate.sdpMid,
                        sdpMLineIndex = event.candidate.sdpMLineIndex,
                    ),
                )
                RtcEvent.PeerConnected -> dispatch(CallEvent.PeerConnected)
                RtcEvent.TransportLost -> dispatch(CallEvent.TransportLost)
                RtcEvent.TransportResumed -> dispatch(CallEvent.TransportResumed)
                RtcEvent.Closed -> Unit // teardown converges via ReleaseCall
            }
        }
    }

    private fun startAnswerTimeout() {
        // docs/03 §3.2's "answer timeout" branch of Signaling → Ended: a
        // worker that accepted the socket but never answers must not hold
        // the caller on a spinner forever.
        answerTimeoutJob = scope.launch {
            delay(answerTimeoutMillis)
            dispatch(CallEvent.Failed(ApiError.UNKNOWN))
        }
    }

    private fun startGraceTimer() {
        graceJob = scope.launch {
            delay(reconnectGraceMillis)
            dispatch(CallEvent.GraceExpired)
        }
    }

    private fun endSessionOnApi() {
        val id = sessionId ?: return
        // Fire-and-forget on the outer scope (not transportJob — that is
        // being torn down): DELETE is idempotent by contract (docs/13 §2.2),
        // and its result cannot change a call that is already Ended.
        scope.launch { api.endSession(id) }
    }

    private fun releaseCall() {
        answerTimeoutJob?.cancel()
        answerTimeoutJob = null
        graceJob?.cancel()
        graceJob = null
        transportJob?.cancel()
        transportJob = null
        scope.launch {
            try {
                webRtc.close()
            } catch (e: CancellationException) {
                throw e
            } catch (_: Exception) {
                // Teardown must complete even if the peer objects to dying;
                // the socket close below must not be skipped.
            }
            signaling.close()
        }
    }

    public companion object {
        /**
         * How long Signaling may wait on the answer. Generous against the
         * ≤ 1.5 s p50 / ≤ 3 s TURN-relayed setup budget (docs/06 §2.1) —
         * this is a stuck-worker backstop, not a latency target.
         */
        public const val ANSWER_TIMEOUT_MILLIS: Long = 10_000L

        /** docs/06 §6: the 30 s reconnect grace. */
        public const val RECONNECT_GRACE_MILLIS: Long = 30_000L
    }
}
