package com.vyaparpay.voice

import com.vyaparpay.core.network.ApiError
import com.vyaparpay.core.network.ApiResult
import com.vyaparpay.core.network.ConnectBundleDto
import com.vyaparpay.core.network.SessionCreateRequestDto
import com.vyaparpay.core.network.VyaparApi
import com.vyaparpay.voice.context.CallContextPublisher
import com.vyaparpay.voice.context.ContextDownlink
import com.vyaparpay.voice.context.ContextDownlinkFrame
import com.vyaparpay.voice.signaling.OutboundSignal
import com.vyaparpay.voice.signaling.SignalFrame
import com.vyaparpay.voice.signaling.SignalingClient
import com.vyaparpay.voice.signaling.SignalingTarget
import com.vyaparpay.voice.webrtc.RemoteIceCandidate
import com.vyaparpay.voice.webrtc.RtcEvent
import com.vyaparpay.voice.webrtc.Sdp
import com.vyaparpay.voice.webrtc.WebRtcClient
import com.vyaparpay.voice.webrtc.WebRtcContextChannel
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineExceptionHandler
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
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
 * and its `candidate: null` marker) → apply answer → media connects → bind the
 * screen-context publisher to the now-open `ctx` channel (docs/03 §3.10) →
 * in call → bye/teardown.
 */
public class CallController(
    private val api: VyaparApi,
    private val signaling: SignalingClient,
    private val webRtc: WebRtcClient,
    private val scope: CoroutineScope,
    /**
     * The screen-context capture pipeline, bound to the `ctx` channel for the
     * life of the call (docs/03 §3.10). Nullable *by design*, not for
     * convenience: docs/08 §7's rule is "degrade context, never conversation",
     * and a null here is that rule expressed in the type — a call with no
     * capture pipeline is a working call with a blind agent, never a failed
     * one. `VoiceCallService` always supplies one; the tests that do not care
     * about context leave it out.
     */
    private val contextPublisher: CallContextPublisher? = null,
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

    /** Non-null between [bindContextPublisher] and [releaseCall]; cancelling it *is* the publisher's `stop()`. */
    private var contextScope: CoroutineScope? = null

    /** Set at [bindContextPublisher] and deliberately never cleared — see [contextFramesDropped]. */
    private var contextChannel: WebRtcContextChannel? = null

    /**
     * Context frames the current (or most recent) call gave up on, because the
     * `ctx` data channel was not OPEN when the publisher tried to ship them —
     * routine during the docs/06 §6 reconnect grace, and non-zero afterwards
     * means the agent spent part of the call reasoning about a stale screen.
     *
     * `:voice` carries no logger (verified: not one `android.util.Log` call in
     * the module), so exposing the count is the honest alternative to
     * pretending it is logged somewhere. Zero when no call has bound a
     * publisher yet. The intended consumer is the `CallViewModel` that lands
     * with the call-trigger UI; until then this is the seam that makes the
     * counter reachable at all rather than test-only.
     */
    public val contextFramesDropped: Long get() = contextChannel?.droppedFrameCount ?: 0L

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
            CallEffect.BindContextPublisher -> bindContextPublisher()
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
            } catch (_: Throwable) {
                // Connect refused or peer bootstrap failed — either way the
                // control plane never came up (docs/03 §3.2's Signaling
                // failure). The cause was a transport/native error, not a
                // wire code, so there is nothing better than UNKNOWN to map.
                //
                // Throwable, not Exception (found live, real device):
                // webRtc.start() does the same real native init
                // (PeerConnectionFactory / libwebrtc) that
                // VoiceCallService.handleStart's own
                // WebRtcClientFactory.create() catch already documents as
                // Error-capable ("e.g. an UnsatisfiedLinkError"), but this
                // catch, one call later in the same native surface, only
                // caught Exception — so an Error here escaped this
                // coroutine, was never dispatched as CallEvent.Failed, and
                // crashed the process outright once serviceScope's
                // SupervisorJob had nothing left to catch it. Confirmed on
                // a real device: the signaling WebSocket connects
                // (voice-worker logs signaling.connected), then the app
                // vanished from the foreground and voice-worker saw
                // ConnectionClosedError with no close code — the signature
                // of the client process dying, not a graceful failure path.
                dispatch(CallEvent.Failed(ApiError.UNKNOWN))
            }
        }
    }

    /**
     * `CallEffect.BindContextPublisher`: attach the capture pipeline to the
     * now-open `ctx` channel, and start pumping the downlink for
     * `ctx.request_snapshot` (docs/03 §3.10, docs/08 §3.3).
     *
     * **Judgment call 1 — an effect handled here, not a `CallState` observer
     * in `VoiceCallCoordinator`.** The reducer's own comment on the
     * `Connecting → PeerConnected` row already framed this as an effect
     * ("binding the context publisher ... are the service and capture tasks'
     * effects"), and this class is where effects execute. It is also the only
     * place that holds all three things the binding needs at once: the
     * [WebRtcClient] the channel adapts, the call scope, and
     * `CallEffect.ReleaseCall` — the single point every terminal path
     * converges on, which is what makes teardown a structural guarantee
     * rather than a second observer that has to be kept in sync. A
     * `VoiceCallCoordinator` observer would have had none of them (it sees
     * `CallState`, `CallAudioSession` and `CallNotifier` only) and would have
     * needed the peer plumbed through it purely to reach `sendContext`. The
     * cost — `CallStateMachine`/`CallEffect` are merged, reviewed files — is
     * one additive effect and the assertions in `CallStateMachineTest` that
     * name it.
     *
     * **Judgment call 2 — a fresh child scope, never [scope] itself, and
     * never the app-scoped `CoroutineScope` `ScreenContextModule` provides.**
     * `ScreenContextPublisher.start` has no `stop()`; cancelling the supplied
     * scope is the *only* teardown mechanism, so the scope's lifetime is the
     * publisher's lifetime. `ScreenContextModule`'s `@Singleton` scope is
     * app-lifetime by design (it belongs to `UiTreeCollector`/
     * `AppStateManager`, which must survive between calls); handing it to the
     * publisher would leave the screen-state collector running after hang-up,
     * still shipping the merchant's screen contents into a dead channel — or,
     * worse, into whatever the *next* call binds. That is a privacy failure,
     * not a leak, and it is the same class of hazard as docs/03 §3.3's "a
     * leaked `PeerConnection` is a live mic". The child scope below is parented
     * to [scope] (so `VoiceCallService.onDestroy`'s `serviceScope.cancel()`
     * still reaches it) but is independently cancellable, so [releaseCall] can
     * end the publisher without ending the dispatch loop that is running
     * [releaseCall].
     *
     * `SupervisorJob` + a swallowing [CoroutineExceptionHandler]: with a plain
     * `Job`, one failing collector would cancel the other *and* propagate up
     * to [scope], taking the call down with it. [WebRtcContextChannel] already
     * absorbs send failures, which is where they overwhelmingly come from —
     * this is the structural backstop that makes "degrade context, never
     * conversation" hold even for a failure mode nobody anticipated, rather
     * than relying on every future collector being individually careful.
     *
     * Idempotent by the `contextScope != null` guard. The reducer only emits
     * this effect on `Connecting → InCall`, and a mid-call
     * `Reconnecting → InCall` resume emits `CancelReconnectGrace` instead —
     * so a double bind is not reachable today. The guard is here because the
     * consequence if it ever became reachable is two publishers racing one
     * `seq` counter over one channel, which the server reads as a permanent
     * gap storm; a one-line invariant is cheaper than that failure.
     */
    private fun bindContextPublisher() {
        val publisher = contextPublisher ?: return
        if (contextScope != null) return

        val publisherScope = CoroutineScope(
            scope.coroutineContext +
                SupervisorJob(scope.coroutineContext[Job]) +
                CONTEXT_FAILURE_HANDLER,
        )
        contextScope = publisherScope

        // Retained, not constructed inline (review fix, MEDIUM). The channel
        // counts frames it had to drop, and [WebRtcContextChannel]'s own kdoc
        // calls that counter "the observability that keeps this from being a
        // silent failure" -- but an instance built inline and dropped on the
        // floor is observable only to its unit test. Holding it here is what
        // makes [contextFramesDropped] real. Deliberately NOT cleared in
        // releaseCall(): the count is most interesting *after* the call, and
        // this adds no retention -- the channel's only field is `webRtc`,
        // which this controller already holds.
        val channel = WebRtcContextChannel(webRtc)
        contextChannel = channel

        publisher.start(channel, publisherScope)
        publisherScope.launch { pumpContextDownlink(publisher) }
    }

    /**
     * The server → client half of the gap-recovery loop (docs/08 §3.3):
     * `ctx.request_snapshot` in, a fresh full snapshot out. Without this the
     * loop is one-way — the server discards a gapped delta and asks for a
     * re-sync that never arrives.
     *
     * `transcript.partial`/`transcript.final`/`agent.state` share this
     * channel and are *not* handled here; they belong to the overlay task
     * (docs/03 §3.12) — see [ContextDownlinkFrame.IGNORED]'s kdoc for why that
     * is a deliberate seam rather than a gap. [WebRtcClient.incoming] is a
     * `SharedFlow`, so that task adds a second collector without disturbing
     * this one.
     *
     * The per-message `try` keeps one bad frame from ending the pump, the
     * same containment `ContextDispatcher` applies on the other end of the
     * wire ("one malformed message degrades context for that message only").
     */
    private suspend fun pumpContextDownlink(publisher: CallContextPublisher) {
        webRtc.incoming.collect { frame ->
            if (ContextDownlink.classify(frame) != ContextDownlinkFrame.REQUEST_SNAPSHOT) return@collect
            try {
                publisher.requestFullSnapshot()
            } catch (e: CancellationException) {
                throw e
            } catch (_: Exception) {
                // A failed recovery snapshot costs one round trip of
                // staleness: the next gapped client message re-triggers
                // `ctx.request_snapshot` (docs/08 §3.2). Never the call.
            }
        }
    }

    private suspend fun collectSignalingFrames() {
        signaling.frames.collect { frame ->
            // Whole-branch try/catch (found live, real device, same session
            // as openTransport's Throwable widening): this collector runs
            // as a sibling coroutine to openTransport's — launched before
            // its try block, never inside it — so that fix covered none of
            // these branches. webRtc.addRemoteCandidate below is the same
            // native PeerConnection surface as webRtc.start/applyAnswer,
            // both already known Error-capable; an uncaught throw here
            // escaped this coroutine exactly like the openTransport case
            // did, with the same outcome (serviceScope's SupervisorJob had
            // nothing left to catch it, the process died). The existing
            // inner try/catch on the Answer branch is left as-is — this is
            // a backstop for the other four, not a replacement for it.
            try {
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
            } catch (e: CancellationException) {
                throw e
            } catch (_: Throwable) {
                dispatch(CallEvent.Failed(ApiError.UNKNOWN))
            }
        }
    }

    private suspend fun collectRtcEvents() {
        webRtc.events.collect { event ->
            // Same backstop as collectSignalingFrames, same reason: a
            // sibling coroutine to openTransport's try/catch, not inside
            // it. signaling.send below can throw (the socket can already
            // be gone by the time the first ICE candidate arrives), and
            // every branch here touches the same native WebRTC surface
            // openTransport's own Throwable fix was written for.
            try {
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
            } catch (e: CancellationException) {
                throw e
            } catch (_: Throwable) {
                dispatch(CallEvent.Failed(ApiError.UNKNOWN))
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
        // First, and before webRtc.close() below: the capture collectors must
        // stop producing before the channel they produce into is disposed,
        // and — the part that actually matters — the merchant's screen must
        // stop leaving the device the moment the call is over, not whenever
        // the coroutine machinery gets around to it. See
        // bindContextPublisher()'s judgment call 2.
        contextScope?.cancel()
        contextScope = null
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

        /**
         * docs/08 §7, structurally: nothing thrown inside the context
         * pipeline may reach the call's scope. Handled by dropping — there is
         * no logger in `:voice`, and `WebRtcContextChannel.droppedFrameCount`
         * already carries the observable half of the story for the failure
         * mode that actually happens (a closed data channel).
         */
        private val CONTEXT_FAILURE_HANDLER = CoroutineExceptionHandler { _, _ -> }
    }
}
