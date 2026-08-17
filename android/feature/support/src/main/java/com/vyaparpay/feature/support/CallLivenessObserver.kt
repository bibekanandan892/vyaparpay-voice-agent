package com.vyaparpay.feature.support

import com.vyaparpay.voice.CallState
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch

/**
 * The one writer of [CallActivitySignal]: watches a call's own state flow for
 * the call's whole life — including after the [CallViewModel] that found it
 * has been destroyed — and publishes live/not-live from it.
 *
 * **Why this exists.** [CallViewModel] is scoped to the Support nav entry and
 * [CallViewModel.onCleared] deliberately stops observing without ending the
 * call. Before this class, the ViewModel was also the signal's writer, which
 * meant that if the call ended while the merchant was on any *other* screen
 * — the agent hung up, or they tapped End in the notification shade —
 * nothing published `false`, and `:app`'s "Return to call" chip stayed
 * offered until they next opened Support. Confirmed as reachable in the most
 * ordinary path by two independent reviewers (2026-08-17); the fix is an
 * observer whose lifetime is the *process*, not a screen.
 *
 * **Why no second `bindService`.** [BoundCall.state] is `CallController.state`
 * — a `StateFlow` owned by the running `VoiceCallService`. Once
 * [CallViewModel] has it in hand it can simply be handed here and collected
 * from a process-lifetime scope: unbinding the ViewModel's `ServiceConnection`
 * releases the *binding*, not the controller, which lives on inside the
 * foreground service (`stopWithTask="false"`) until the call is over. So the
 * observation continues, VM or no VM, exactly as long as the call does. That
 * avoids a second, independent binding — with the same controller-attach
 * retry dance `CallViewModel.handleControllerlessBinding` already needs —
 * solely to learn something the ViewModel already knows the moment it binds.
 *
 * **What is published.** `true` for every state except [CallState.Ended],
 * `false` on `Ended`, after which the collection stops and the flow is
 * dropped (holding a finished call's controller would keep it reachable for
 * no reason). [CallState.Idle] counts as live on purpose: [track] is only
 * ever called with a call the ViewModel bound during or after a start (or
 * discovered already running), so an `Idle` first emission there is a
 * controller that has not yet processed its start intent — the same
 * "still connecting, not idle" reading `CallViewModel.onCallStateChanged`
 * gives it — not a call that does not exist. Because [track] is called
 * from the ViewModel's own bind path, `true` first appears one
 * `bindService` round-trip after "Call Support" is tapped; the merchant is
 * normally still on the Support route at that instant, where the chip is
 * hidden anyway. The one narrow gap: a ViewModel cleared *inside* that
 * round-trip (or during `CallViewModel.handleControllerlessBinding`'s
 * retry gap) never reaches [track] for that call, so the chip is absent
 * until the next ViewModel's reconnaissance bind hands the call over —
 * sub-second to hit, self-healing on the next visit to Support, accepted
 * (see the note at the call site in `CallViewModel.onBound`).
 *
 * **What is deliberately not written.** A ViewModel whose reconnaissance
 * bind finds *nothing* running does not publish `false` through here — it
 * has no flow to hand over, and publishing on "I saw nothing" would race a
 * live call's own `true` (a bind can connect before the service has
 * processed a start). Whatever call last ran has already driven the signal
 * to `false` on its own `Ended`; a process that was killed mid-call comes
 * back with the signal at its `false` default and the call gone with the
 * process. Both are correct without a second writer.
 *
 * **Threading.** [scope] is `Dispatchers.Main.immediate` in production, the
 * same confinement [CallViewModel]'s own collectors run under, so
 * [CallActivitySignal.update] is always a main-thread write and [job] needs
 * no synchronization. Injected so tests drive it with a `TestScope`.
 */
@Singleton
public class CallLivenessObserver internal constructor(
    private val signal: CallActivitySignal,
    private val scope: CoroutineScope,
) {

    /** The Hilt entry point; production observes on the main dispatcher for the process lifetime. */
    @Inject
    public constructor(signal: CallActivitySignal) :
        this(signal, CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate))

    /** The active collection, if any. Main-confined. */
    private var job: Job? = null

    /**
     * Start (or restart) publishing from [callState] until it reaches
     * [CallState.Ended]. Idempotent in effect: a ViewModel re-binding to the
     * same running call (merchant returns to Support mid-call) simply
     * replaces the collector with an equivalent one — the published value is
     * the same either way, and only ever one collector runs at a time.
     */
    internal fun track(callState: StateFlow<CallState>) {
        job?.cancel()
        job = scope.launch {
            // `launch` returns this very Job; under Main.immediate the body
            // can run inline BEFORE `job = ...` above is assigned, so the
            // handle is compared by identity rather than nulled blindly.
            val self = coroutineContext[Job]
            callState.collect { state ->
                val live = state !is CallState.Ended
                signal.update(live)
                if (!live) {
                    // Nothing further can be published from a finished call
                    // (CallStateMachine holds Ended steady, and a StateFlow
                    // never re-emits an equal value); stop holding it.
                    if (job === self) job = null
                    // Cancelling from inside the collector throws
                    // CancellationException out of collect — how a coroutine
                    // ends itself.
                    self?.cancel()
                }
            }
        }
    }

    /**
     * The service went away WITHOUT a terminal state -- `onServiceDisconnected`
     * ([CallViewModel.onUnbound]), which for this same-process service means
     * the process itself died and nothing is running any more. The tracked
     * flow will never emit `Ended`, so publish not-live now and drop it.
     * Unreachable in-process today (a dead process takes this singleton
     * with it); kept symmetric with the ViewModel's own "end honestly"
     * handling so the two cannot drift if the service ever moves out of
     * process, where this becomes the only thing that hides the chip.
     */
    internal fun abandon() {
        job?.cancel()
        job = null
        signal.update(false)
    }
}
