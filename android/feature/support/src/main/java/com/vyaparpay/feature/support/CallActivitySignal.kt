package com.vyaparpay.feature.support

import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * Whether a call is currently live, for a "return to call" affordance hosted
 * above `:app`'s `NavHost` — the exact placement gap [SupportButton]'s own
 * kdoc names for the floating FAB, and the same one a return-to-call chip
 * needs solved: something outside this module needs to know a call is
 * running without owning any part of driving it.
 *
 * **`@Singleton`, one instance for the process.** [CallViewModel] is scoped
 * to wherever `hiltViewModel()` resolves it (effectively the `HelpScreen`
 * nav entry); the chip needs the same fact from `:app`'s composition root,
 * which lives above that scope entirely and outlives it exactly the way
 * [CallViewModel.onCleared]'s own kdoc says the call itself does.
 *
 * **[CallLivenessObserver] is the only writer, by construction.** [update] is
 * `internal` — nothing outside this module can call it — and inside the
 * module the one caller is [CallLivenessObserver.track], a process-lifetime
 * collector of the call's own state flow. `isCallLive` is the only thing
 * exposed outward, and only for reading.
 *
 * **Why the writer is not [CallViewModel].** It was, in the first version,
 * and that was a real bug: [CallViewModel.onCleared] deliberately stops
 * observing without ending the call, so a call that ended while the merchant
 * was on any other screen — agent hung up, End tapped in the notification
 * shade — had nobody left to publish `false`, and the chip stayed offered
 * until they next opened Support. Now the observer is handed the call's flow
 * the moment a ViewModel binds and keeps collecting it after that ViewModel
 * is gone, so `false` lands on the call's own `Ended` wherever the merchant
 * happens to be. What remains is only process death, which takes the call
 * with it and restarts this at its `false` default — the correct answer.
 */
@Singleton
public class CallActivitySignal @Inject constructor() {

    private val _isCallLive = MutableStateFlow(false)

    /** Whether there is a call a "return to call" affordance should offer to jump back into. */
    public val isCallLive: StateFlow<Boolean> = _isCallLive.asStateFlow()

    internal fun update(live: Boolean) {
        _isCallLive.value = live
    }
}
