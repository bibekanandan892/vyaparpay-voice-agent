package com.vyaparpay.feature.support

import android.content.Context
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.vyaparpay.core.network.SessionCreateRequestDto
import com.vyaparpay.core.screencontext.AppStateManager
import com.vyaparpay.voice.CallState
import dagger.hilt.android.lifecycle.HiltViewModel
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.serialization.json.Json

/**
 * docs/03 §3.12's `CallViewModel`: the trigger that turns a "Call Support" tap
 * into a running call, and the bound client that watches it.
 *
 * **This class is the first production injection point for [AppStateManager].**
 * Until it existed, nothing in the app constructed one outside a test — the
 * capture pipeline aggregated a screen-context timeline that no code path ever
 * read. [startCall] is where that half of the system finally does work: the
 * `POST /v1/sessions` body is [AppStateManager.sessionCreateBody]'s output,
 * unmodified. Nothing here fabricates, defaults, or patches that body —
 * `CallViewModelSessionBodyTest` proves it by driving a real Compose capture
 * through a real [AppStateManager] and asserting the emitted request carries
 * that screen and event timeline. It matters because a session body assembled
 * here instead of there would silently defeat capture exclusion (docs/07
 * §2.1), and the agent would open the call looking at `HelpScreen` rather than
 * at whatever the merchant was actually stuck on.
 *
 * **The sequence.** `VoiceCallService` owns every step after the request: it
 * mints the session through `CallController.mintSession`, opens signaling, and
 * brings up media. This class never touches `VyaparApi`. Two distinct bindings
 * follow from that, and only one of them is ordered against a start (LOW-2,
 * second independent review of T8c — an earlier version of this paragraph
 * claimed a bind never precedes a start, which the reconnaissance bind below
 * contradicts):
 *
 * - **Within [startCall], start strictly precedes bind.** The start intent is
 *   what *creates* the call, so binding first would find no controller to
 *   observe. `CallViewModelTest` asserts that ordering against
 *   `FakeVoiceCallLauncher`'s single `callLog`.
 * - **The `init` reconnaissance bind has no start behind it, deliberately.**
 *   It exists to *discover* a call this ViewModel did not start (see the
 *   reconciliation paragraph below). A controller-less answer there is the
 *   expected one, not a failure — which is exactly the distinction
 *   [attachAttemptsLeft] encodes.
 *
 * **Threading.** [BoundCall.state] is a `StateFlow` fed from the service's own
 * confined dispatcher; the collector below runs on [viewModelScope] (main), so
 * every [state] update is main-thread, which is what Compose wants. The
 * `ServiceConnection` callbacks likewise arrive on main, so [call], [stateJob]
 * and [pendingHangUp] are main-confined and need no synchronization — the same
 * confinement argument `VoiceCallCoordinator` documents for its own
 * non-`@Volatile` fields.
 *
 * **Public class, module-internal control surface.** Only [state] and the
 * class itself are public — and the class only because [SupportRoute]'s public
 * signature names it. [startCall], [hangUp], [dismiss] and
 * [onPermissionDecision] are all `internal`, so nothing outside this module
 * can reach [startCall] without going through [rememberCallPermissionGate]
 * first. That is not tidiness: `VoiceCallService` is a microphone-typed
 * foreground service, and starting one on Android 14+ without RECORD_AUDIO
 * already granted throws in the *host app*. Making the bypass unrepresentable
 * is cheaper than documenting that it must not be done.
 *
 * **Reconciliation on creation (HIGH fix, independent review of T8c).** The
 * call outlives this ViewModel by design: the service runs with
 * `stopWithTask="false"`, and `:app`'s NavHost gives the Support route no
 * state saving, so leaving the screen mid-call destroys this ViewModel while
 * the mic stays open. Before this fix nothing ever re-bound — a merchant
 * returning to Help found a blank panel over a live call, with the
 * notification's End action as the only way out. The `init` block therefore
 * binds *unconditionally* on creation, and the controller-less outcome is
 * disambiguated by [attachAttemptsLeft]: zero means this was reconnaissance
 * (nothing is running — stay [CallPhase.IDLE], quietly release), non-zero
 * means a start is in flight (see [handleControllerlessBinding] for why that
 * needs retries rather than an immediate verdict).
 */
@HiltViewModel
public class CallViewModel internal constructor(
    private val appState: AppStateManager,
    private val launcher: VoiceCallLauncher,
) : ViewModel() {

    /**
     * The Hilt entry point, matching [HelpViewModel]'s established shape: the
     * primary constructor takes the testable seam, and this one adapts the
     * graph to it. [AppStateManager] is a `@Singleton` with its own `@Inject`
     * constructor, so Hilt builds it (and the capture pipeline beneath it)
     * without a module here.
     *
     * `@ApplicationContext`, never an `Activity` context: [AndroidVoiceCallLauncher]
     * holds it for the life of the binding, which deliberately outlives the
     * composition that started the call.
     */
    @Inject
    public constructor(
        appState: AppStateManager,
        @ApplicationContext context: Context,
    ) : this(appState, AndroidVoiceCallLauncher(context))

    private val _state = MutableStateFlow(CallUiState())
    public val state: StateFlow<CallUiState> = _state.asStateFlow()

    /** The bound call, or `null` when not bound. Main-confined. */
    private var call: BoundCall? = null

    /** Collects [BoundCall.state]; replaced on rebind, cancelled on unbind. */
    private var stateJob: Job? = null

    /**
     * A hang-up that arrived before the binding did.
     *
     * `bindService` is asynchronous, so there is a real window — short, but
     * reachable on a cold start behind a slow service creation — where the
     * merchant sees "Connecting…" with an End button and no bound [call] to
     * route it to. Dropping the tap there would leave a live mic running with
     * the UI insisting the user already ended it, which is the exact class of
     * failure docs/03 §2.2 calls out. Replaying it on connect is the fix.
     */
    private var pendingHangUp: Boolean = false

    /**
     * Controller-attach retries the current start attempt may still consume.
     *
     * Zero whenever no start is in flight — which is exactly what routes a
     * controller-less *reconnaissance* bind (the `init` one) to "stay idle"
     * instead of "declare a call that was never started failed". Set by
     * [startCall], drained by [handleControllerlessBinding], zeroed the moment
     * a real controller attaches. Main-confined like every other field here.
     */
    private var attachAttemptsLeft: Int = 0

    /**
     * **Placement is load-bearing — keep this block last (LOW-4, second
     * independent review of T8c).**
     *
     * [launcher] `bind` can invoke [onBound] *synchronously*: the MEDIUM fix
     * in [AndroidVoiceCallLauncher.bind] reports `onBound(null)` inline when
     * `bindService` refuses. So this constructor can reach [onBound] — and
     * through it write [call], [stateJob], [pendingHangUp] and
     * [attachAttemptsLeft] — before construction finishes. Every one of those
     * declarations must therefore be *above* this block, or its initializer
     * would run afterwards and silently overwrite what the callback just
     * wrote.
     *
     * That hazard is currently masked: Kotlin elides initializers that only
     * store a JVM default (`null`/`false`/`0`), which is what all four happen
     * to be, so a disassembly of this constructor shows no such writes today.
     * Masked is not fixed — giving any of those fields a non-default initial
     * value, or adding a new field [onBound] touches, brings the clobber
     * straight back. Ordering makes it correct by construction instead of by
     * coincidence.
     *
     * What the bind is *for*: reconciliation with an already-running call, per
     * the class kdoc's HIGH-fix paragraph. `BIND_AUTO_CREATE` means it also
     * creates (and, via the controller-less branch, promptly releases) the
     * service when nothing is running; that create/observe/destroy round trip
     * is the deliberate price of never showing a blank panel over a live mic.
     * The service is bound-only throughout — never started — so no
     * foreground-service obligations attach (`VoiceCallService` only takes
     * those on through `ACTION_START`).
     */
    init {
        launcher.bind(onBound = ::onBound, onUnbound = ::onUnbound)
    }

    /**
     * Route a completed permission round trip (docs/03 §3.6).
     *
     * The notifications branch is a *notice*, not a gate: it is applied on the
     * way through to [startCall], never instead of it. See [PermissionManager]'s
     * kdoc for why POST_NOTIFICATIONS never blocks a call.
     */
    internal fun onPermissionDecision(decision: CallPermissionDecision) {
        when (decision.outcome) {
            CallPermissionOutcome.PROCEED -> {
                _state.update {
                    it.copy(notice = if (decision.notificationsGranted) null else CallNotice.NOTIFICATIONS_DENIED)
                }
                startCall()
            }
            CallPermissionOutcome.RETRYABLE_DENIAL ->
                _state.update { it.copy(notice = CallNotice.MIC_DENIED) }
            CallPermissionOutcome.PERMANENT_DENIAL ->
                _state.update { it.copy(notice = CallNotice.MIC_BLOCKED) }
        }
    }

    /**
     * Build the session request from live app state, hand it to
     * `VoiceCallService`, and bind to watch the result.
     *
     * Guarded against a double tap: a second start while a call is already in
     * flight is dropped here rather than relying on `VoiceCallService.handleStart`'s
     * own `controller != null` guard, so the UI never flickers back to
     * "connecting" over a call that is already up.
     */
    internal fun startCall() {
        if (_state.value.canHangUp) return

        val request = appState.sessionCreateBody(DEMO_USER_ID)
        attachAttemptsLeft = CONTROLLER_ATTACH_ATTEMPTS
        _state.update { it.copy(phase = CallPhase.CONNECTING, endReason = null, contextFramesDropped = 0L) }

        // Default Json configuration on purpose: VoiceCallService.decodeSessionRequest
        // decodes with a bare `Json`, and the two must agree.
        // SessionCreateRequestDto has no defaulted fields, so
        // `encodeDefaults = false` cannot drop anything the server needs.
        val started = launcher.start(Json.encodeToString(SessionCreateRequestDto.serializer(), request))

        if (!started) {
            // HIGH fix (second independent review of T8c). The platform
            // refused the foreground-service start — reachable whenever the
            // process backgrounds between the tap and the asynchronously
            // dispatched permission result (see AndroidVoiceCallLauncher.start).
            //
            // Two things had to be fixed here, not one. The launcher no longer
            // lets the exception escape into the ActivityResult dispatch and
            // crash the host app; this branch closes the other half, the
            // wedge. The lines above have ALREADY moved the UI to CONNECTING
            // and armed attachAttemptsLeft, and there is now no service to
            // bind to — so returning without this would leave phase=CONNECTING
            // with canHangUp=true forever: no callback to resolve it, the
            // retry loop never armed (bind never ran), and both startCall and
            // dismiss refused by their own canHangUp gates. Unrecoverable
            // without killing the app, and precisely the "permanent
            // Connecting…" the MEDIUM fix exists to eliminate.
            //
            // Returning before the bind is also required, not just tidy:
            // binding now would use BIND_AUTO_CREATE to conjure the very
            // service the platform just refused to let us start.
            reportCallFailed()
            return
        }

        // A no-op when the init-block reconnaissance binding is still pending
        // (the launcher refuses a second connection) — that binding's callback
        // then serves this start, which is why both use the same ::onBound.
        launcher.bind(onBound = ::onBound, onUnbound = ::onUnbound)
    }

    /**
     * The one terminal-failure path: release everything and report an honest
     * ended call.
     *
     * Shared by the refused-start branch of [startCall] and the exhausted
     * retries branch of [handleControllerlessBinding], so both failures leave
     * identical, *recoverable* state — [releaseBinding] zeroes
     * [attachAttemptsLeft], and [CallPhase.ENDED] has `canHangUp == false`,
     * which is what lets [dismiss] clear the surface and a later [startCall]
     * try again.
     *
     * `endReason` stays `null` rather than borrowing `EndReason.SETUP_FAILED`:
     * these calls never reached `:voice`, so there is no reason it reported.
     * `CallStatusPanel` already documents `null` as "something finished and we
     * will not invent a cause".
     */
    private fun reportCallFailed() {
        releaseBinding()
        _state.update { it.copy(phase = CallPhase.ENDED, endReason = null) }
    }

    /** The merchant tapped End. */
    internal fun hangUp() {
        val bound = call
        if (bound == null) {
            // Not bound yet — see pendingHangUp's kdoc. Deliberately NOT sent
            // as an ACTION_END intent: startService on a service that is not
            // running would create one that never reaches startForeground,
            // which is a leaked idle service (VoiceCallService.onStartCommand's
            // own else-branch exists to close that same hazard from the other
            // side).
            pendingHangUp = true
            return
        }
        bound.hangUp()
    }

    /** Dismiss a terminal call summary or a permission notice, returning the surface to nothing. */
    internal fun dismiss() {
        if (_state.value.canHangUp) return
        _state.value = CallUiState()
    }

    private fun onBound(bound: BoundCall?) {
        if (bound == null) {
            handleControllerlessBinding()
            return
        }

        attachAttemptsLeft = 0
        call = bound
        if (pendingHangUp) {
            pendingHangUp = false
            bound.hangUp()
        }

        stateJob?.cancel()
        stateJob = viewModelScope.launch {
            bound.state.collect { callState -> onCallStateChanged(bound, callState) }
        }
    }

    /**
     * The binding connected, but `LocalBinder.callController` was null. What
     * that *means* depends on who asked.
     *
     * **Reconnaissance ([attachAttemptsLeft] == 0):** nothing is running —
     * the expected answer on almost every visit to the screen. Release the
     * binding and stay [CallPhase.IDLE]. Reporting ENDED here (as the
     * pre-review code did unconditionally) would greet every merchant who
     * opens Help with "Call ended" for a call that never existed.
     *
     * **A start in flight ([attachAttemptsLeft] > 0):** ambiguous, and this is
     * the racy branch the retry loop exists for. A null controller here means
     * *either* the start was rejected (malformed payload / native init failure
     * — both `stopSelf` without ever setting the controller, and with this
     * client still bound via BIND_AUTO_CREATE the service stays alive in that
     * controller-less state, so no `onServiceDisconnected` will ever break the
     * tie) *or* `ACTION_START` simply has not been processed yet — Android
     * makes no ordering promise between `onServiceConnected` and a
     * concurrently delivered start command, and the reconnaissance bind makes
     * the pre-start window genuinely reachable (tap Call Support while the
     * probe's service creation is still in flight). One immediate verdict
     * cannot distinguish the two, so: release, rebind after a beat, and let
     * the controller's presence decide. `handleStart` assigns the controller
     * synchronously inside `onStartCommand` — including its WebRTC native
     * init — so [CONTROLLER_ATTACH_ATTEMPTS] × [CONTROLLER_ATTACH_RETRY_MILLIS]
     * comfortably covers the legitimate path, and exhaustion means the reject
     * path: report ENDED honestly rather than spin forever.
     *
     * The scheduled rebind needs no staleness guard beyond [viewModelScope]:
     * a second [startCall] is unreachable while CONNECTING (the `canHangUp`
     * gate), [dismiss] refuses non-terminal states, and [onCleared] cancels
     * the scope before the delay fires.
     */
    private fun handleControllerlessBinding() {
        launcher.unbind()

        if (attachAttemptsLeft == 0) return // Reconnaissance: nothing running, nothing to show.

        attachAttemptsLeft--
        if (attachAttemptsLeft == 0) {
            reportCallFailed()
            return
        }

        viewModelScope.launch {
            delay(CONTROLLER_ATTACH_RETRY_MILLIS)
            launcher.bind(onBound = ::onBound, onUnbound = ::onUnbound)
        }
    }

    private fun onCallStateChanged(bound: BoundCall, callState: CallState) {
        // A bound controller reporting Idle means only that the service's
        // event loop has not yet drained the SupportTapped that startCall
        // produced — `VoiceCallService.handleStart` dispatches it through a
        // channel, so the first value this collector sees is routinely the
        // machine's initial state. `CallStateMachine` never returns to Idle
        // afterwards (it is the initial state and nothing transitions back
        // into it), so folding it forward here is unambiguous rather than a
        // guess. Reporting IDLE instead would bounce the UI out of
        // "Connecting…" for a frame and, worse, briefly re-open startCall's
        // in-flight guard to a second tap.
        val phase = callState.toPhase().let { if (it == CallPhase.IDLE) CallPhase.CONNECTING else it }

        _state.update {
            it.copy(
                phase = phase,
                endReason = (callState as? CallState.Ended)?.reason,
                // A plain property read on the retained WebRtcContextChannel —
                // cheap enough to refresh on every transition, and the value is
                // only meaningful once frames have actually flowed.
                contextFramesDropped = bound.contextFramesDropped,
            )
        }

        if (callState is CallState.Ended) {
            // Unbinding here is load-bearing, not tidiness. VoiceCallService
            // ends a call by calling stopSelf(), and a *bound* service is not
            // destroyed by its own stopSelf until every client has unbound —
            // so holding this binding past the terminal state would keep the
            // service (and its notification) alive indefinitely. The UI keeps
            // showing the ended state from the snapshot above; there is
            // nothing further to observe.
            releaseBinding()
        }
    }

    private fun onUnbound() {
        // onServiceDisconnected: the service process died without a terminal
        // CallState ever arriving. docs/03 §7's rule is to end the call
        // honestly rather than fake a live one, so the UI goes to ENDED
        // instead of sitting on a spinner backed by nothing.
        releaseBinding()
        _state.update {
            if (it.canHangUp) it.copy(phase = CallPhase.ENDED, endReason = null) else it
        }
    }

    private fun releaseBinding() {
        stateJob?.cancel()
        stateJob = null
        call = null
        pendingHangUp = false
        attachAttemptsLeft = 0
        launcher.unbind()
    }

    /**
     * The one place the binding is guaranteed to be released.
     *
     * A `ServiceConnection` that outlives its owner is a genuine leak — the
     * framework holds a strong reference to it (and through it to this
     * ViewModel) until `unbindService`, and it keeps the bound service alive
     * besides. [releaseBinding] is idempotent and [VoiceCallLauncher.unbind]
     * is a no-op when nothing is bound, so this is safe whether the call ended
     * normally, never started, or is still running when the merchant leaves.
     *
     * Note that this does **not** end the call: the merchant navigating away
     * from `HelpScreen` must not hang up on the agent mid-sentence. The
     * service keeps running with its notification; only the observation stops.
     */
    override fun onCleared() {
        releaseBinding()
        super.onCleared()
    }

    public companion object {
        /**
         * The canonical demo merchant (`backend/scripts/seed.py`'s
         * `MERCHANT_ID`, and the persona every fixture in this repo is built
         * around — docs/01 §6's Rajesh Kumar).
         *
         * **A demo stand-in for a real identity, stated plainly.** This app
         * has no auth: no login screen, no token store, no `sub` claim to read
         * (verified — nothing in the codebase mints or holds a user identity).
         * `SessionCreateRequestDto.userId`'s own kdoc says the server checks
         * it against the JWT `sub`, so the moment authentication lands this
         * constant is replaced by the authenticated principal and nothing else
         * about this class changes. Naming it here rather than inlining the
         * literal is the same honest-seeding convention `SeededPaymentRepository`
         * and `SeededSupportRepository` follow: the fixture is visible and
         * greppable, not disguised as production behaviour.
         */
        internal const val DEMO_USER_ID: String = "usr_rajesh01"

        /**
         * How many controller-less connects a start attempt tolerates before
         * it is declared dead — see [handleControllerlessBinding].
         *
         * **What actually protects the slow path is main-thread
         * serialization, not this budget (LOW-3, second independent review of
         * T8c).** An earlier version of this comment justified ~2 s against
         * `handleStart`'s synchronous WebRTC native init on a cold device.
         * That reasoning was wrong in a way worth correcting, because a
         * maintainer trusting it would believe a >2 s native init times out
         * over a live call. It cannot: `:voice` declares no `android:process`,
         * so `onStartCommand` runs on the main looper, and this retry's
         * `delay` continuation resumes on `Dispatchers.Main` — while
         * `handleStart` blocks the main thread, **no retry can run and no
         * attempt is consumed**. The budget is therefore safer than that text
         * claimed, and it is not racing native init at all.
         *
         * What the budget really counts is main-looper turnarounds while the
         * `ACTION_START` message sits *undelivered* behind our
         * `onServiceConnected` — the genuine ordering ambiguity. Each retry
         * yields the thread for [CONTROLLER_ATTACH_RETRY_MILLIS], letting
         * pending looper messages drain; in practice a queued start lands on
         * the very first turnaround, so 10 is generous headroom rather than a
         * tuned latency figure. Exhaustion means the reject path (the service
         * refused the start and will never set a controller), where the ~2 s
         * is spent under a "Connecting…" spinner before an honest "Call
         * ended".
         */
        internal const val CONTROLLER_ATTACH_ATTEMPTS: Int = 10

        /** Gap between controller-attach rebinds — see [CONTROLLER_ATTACH_ATTEMPTS]. */
        internal const val CONTROLLER_ATTACH_RETRY_MILLIS: Long = 200L
    }
}
