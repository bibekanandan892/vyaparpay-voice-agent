package com.vyaparpay.feature.dashboard

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.vyaparpay.core.analytics.EventTracker
import com.vyaparpay.core.analytics.RingBufferEventTracker
import com.vyaparpay.core.network.ApiResult
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
 * Not a `@HiltViewModel`, for the same reason `PaymentViewModel` (the
 * reference pattern this class follows) is not one: `:feature:dashboard`
 * carries no Hilt dependency, and `:app`'s graph does not bind
 * [DashboardRepository]. [events] defaults to [RingBufferEventTracker] --
 * the real `@Singleton` `EventTracker` that landed in Phase-4 T2, after
 * `PaymentViewModel` was first written against a screen-local ring-buffer
 * stand-in (`InMemoryEventTracker`) because no shared implementation existed
 * yet. That gap is closed now, so this class uses the real one directly
 * instead of duplicating the stand-in a second time.
 */
public class DashboardViewModel @JvmOverloads constructor(
    private val dashboard: DashboardRepository = SeededDashboardRepository(),
    public val events: EventTracker = RingBufferEventTracker(),
) : ViewModel() {

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
