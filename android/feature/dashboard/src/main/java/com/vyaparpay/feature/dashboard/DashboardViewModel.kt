package com.vyaparpay.feature.dashboard

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.vyaparpay.core.analytics.EventTracker
import com.vyaparpay.core.analytics.RingBufferEventTracker
import com.vyaparpay.core.network.ApiResult
import dagger.hilt.android.lifecycle.HiltViewModel
import javax.inject.Inject
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/**
 * Loads `DashboardScreen`'s content on construction and drives its
 * quick-action taps (docs/03-android-architecture.md §2, §4).
 *
 * [dashboard] is the [DashboardRepository] seam: today it defaults to
 * [SeededDashboardRepository], the ₹18,450 fixture `backend/scripts/seed.py`
 * seeds server-side. Swapping in a Retrofit-backed implementation once
 * `VyaparApi` grows `/v1/wallet` + `/v1/settlements` calls is a
 * constructor-argument change -- the state machine below does not change
 * shape.
 *
 * **Now `@HiltViewModel` (Phase-4 T8a).** `:app`'s graph still does not bind
 * [DashboardRepository] (repository seeding is unchanged, out of scope), but
 * `:core:analytics`'s `AnalyticsModule` binds the real, shared `EventTracker`
 * singleton (`RingBufferEventTracker`) that the `@Inject` constructor below
 * now resolves from Hilt. [events]'s constructor default above still
 * constructs its own screen-local [RingBufferEventTracker] instance (a
 * fresh, non-shared ring buffer, kept only for direct/non-DI construction —
 * see `PaymentViewModel`'s (`:feature:payments`) `@Inject` constructor kdoc for why a default
 * can't itself be what Hilt calls); `DashboardRoute`'s `hiltViewModel()` call
 * always resolves the `@Inject` constructor instead, so production code gets
 * the real shared instance every time.
 *
 * **Why the `@Inject` constructor's delegation spells out
 * `SeededDashboardRepository()` again instead of writing `this(events =
 * events)`.** Kotlin allows the primary constructor above to be called with
 * ONLY `events` named (`dashboard` filled from its own default) — the exact
 * same call shape the `@Inject` constructor's signature also accepts. Trying
 * to delegate with `this(events = events)` is therefore genuinely ambiguous
 * between "the primary constructor, `dashboard` defaulted" and "this same
 * constructor, recursively," and the Kotlin compiler resolves that ambiguity
 * by treating it as self-delegation — a compile error ("cycle in the
 * delegation calls chain"), reproduced and confirmed while building this
 * task before landing on the fix below. Supplying `dashboard` explicitly
 * makes the call unambiguously 2-argument, matching only the primary. The
 * duplication is honest, not accidental: [DashboardRepository] wiring is
 * genuinely unchanged (same seeded fixture, same default expression), just
 * written twice because Dagger/Hilt cannot call a constructor "with some
 * arguments defaulted" the way ordinary Kotlin call sites can.
 */
@HiltViewModel
public class DashboardViewModel @JvmOverloads constructor(
    private val dashboard: DashboardRepository = SeededDashboardRepository(),
    public val events: EventTracker = RingBufferEventTracker(),
) : ViewModel() {

    @Inject
    public constructor(events: EventTracker) : this(dashboard = SeededDashboardRepository(), events = events)

    private val _state = MutableStateFlow(DashboardUiState())
    public val state: StateFlow<DashboardUiState> = _state.asStateFlow()

    init {
        viewModelScope.launch { load() }
    }

    private suspend fun load() {
        when (val result = dashboard.getDashboard()) {
            is ApiResult.Success -> onLoaded(result.data)
            is ApiResult.Failure -> onLoadFailed(result)
        }
    }

    private fun onLoaded(snapshot: DashboardSnapshot) {
        _state.update {
            it.copy(
                isLoading = false,
                walletBalancePaise = snapshot.walletBalancePaise,
                collectionsTodayPaise = snapshot.collectionsTodayPaise,
                settlementEtaLabel = snapshot.settlementEtaLabel,
                alertMessage = snapshot.alertMessage,
            )
        }
    }

    private fun onLoadFailed(failure: ApiResult.Failure) {
        // The failure message becomes the screen's one alert_banner slot
        // (kyc_alert) -- a failed load is exactly the kind of thing the role
        // exists to surface ("something the user is being told", docs/07 §3).
        _state.update { it.copy(isLoading = false, alertMessage = failure.message) }
    }
}
