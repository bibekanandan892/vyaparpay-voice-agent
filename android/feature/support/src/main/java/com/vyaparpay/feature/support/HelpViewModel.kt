package com.vyaparpay.feature.support

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
 * Loads `HelpScreen`'s FAQ hub and complaint history on construction
 * (docs/03-android-architecture.md §2).
 *
 * [support] is the [SupportRepository] seam, defaulting to
 * [SeededSupportRepository]. **Now `@HiltViewModel` (Phase-4 T8a)**, matching
 * `PaymentViewModel`/`DashboardViewModel`: [SupportRepository] still has no
 * Hilt module (unchanged, out of scope), but `:core:analytics`'s
 * `AnalyticsModule` binds the real, shared `EventTracker` singleton the
 * `@Inject` constructor below resolves. [events]'s constructor default still
 * builds its own screen-local [RingBufferEventTracker] instance, kept for
 * direct/non-DI construction only -- see `PaymentViewModel`'s
 * (`:feature:payments`) `@Inject` constructor kdoc for the full reasoning,
 * including why the delegation below spells out [SeededSupportRepository]
 * explicitly rather than writing `this(events = events)` (a genuine
 * self-delegation ambiguity Kotlin rejects at compile time otherwise).
 *
 * **This class has no capture-exclusion logic, and needs none.**
 * `SupportDestination.IS_CAPTURED == false` is the entire contract --
 * `NavigationTracker`'s own comment on the `HelpScreen -> "support"`
 * route-to-flow row says the same thing from the other side of the boundary:
 * "that exclusion is ScreenContextPublisher's job, out of scope here". A
 * future `ScreenContextPublisher`/`AppStateManager` gate that refuses to
 * overwrite the last *operational* snapshot while the user is on this route
 * is what makes the exclusion real; nothing in this ViewModel needs to know
 * that gate exists. See [HelpScreen]'s kdoc for the full reconciliation.
 */
@HiltViewModel
public class HelpViewModel @JvmOverloads constructor(
    private val support: SupportRepository = SeededSupportRepository(),
    public val events: EventTracker = RingBufferEventTracker(),
) : ViewModel() {

    @Inject
    public constructor(events: EventTracker) : this(support = SeededSupportRepository(), events = events)

    private val _state = MutableStateFlow(HelpUiState())
    public val state: StateFlow<HelpUiState> = _state.asStateFlow()

    init {
        viewModelScope.launch { load() }
    }

    private suspend fun load() {
        when (val result = support.getHelpContent()) {
            is ApiResult.Success -> _state.update {
                it.copy(isLoading = false, faqs = result.data.faqs, complaints = result.data.complaints)
            }
            is ApiResult.Failure -> _state.update { it.copy(isLoading = false) }
        }
    }
}
