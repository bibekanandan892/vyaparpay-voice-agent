package com.vyaparpay.feature.payments

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.vyaparpay.core.analytics.EventTracker
import com.vyaparpay.core.network.ApiResult
import dagger.hilt.android.lifecycle.HiltViewModel
import javax.inject.Inject
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/**
 * Drives `OrdersScreen` — loads the device-orders page once on construction,
 * the same read-only-list pattern [SettlementsViewModel] uses and for the
 * identical reason (there is no user-triggered reload path, matching the
 * read-only `GET /v1/orders` this stands in for, docs/13 §3.4).
 *
 * **Now `@HiltViewModel` (Phase-4 T8a)** — see [PaymentViewModel]'s kdoc for
 * why: [OrdersRepository] still has no Hilt module (unchanged, out of
 * scope), but [EventTracker] does now — and for why the `@Inject`
 * constructor's delegation below spells out [SeededOrdersRepository]
 * explicitly rather than writing `this(events = events)` (a genuine
 * self-delegation ambiguity Kotlin rejects at compile time otherwise).
 */
@HiltViewModel
public class OrdersViewModel @JvmOverloads constructor(
    private val orders: OrdersRepository = SeededOrdersRepository(),
    public val events: EventTracker = InMemoryEventTracker(),
) : ViewModel() {

    @Inject
    public constructor(events: EventTracker) : this(orders = SeededOrdersRepository(), events = events)

    private val _state = MutableStateFlow(OrdersUiState())
    public val state: StateFlow<OrdersUiState> = _state.asStateFlow()

    init {
        viewModelScope.launch {
            when (val result = orders.getOrders(limit = PAGE_LIMIT)) {
                is ApiResult.Success -> onLoaded(result.data)
                is ApiResult.Failure -> _state.update { it.copy(isLoading = false) }
            }
        }
    }

    private fun onLoaded(page: OrdersPage) {
        _state.update {
            it.copy(
                isLoading = false,
                rows = page.items.map { it.toRowUiModel() },
                totalCount = page.totalCount,
            )
        }
    }

    private companion object {
        const val PAGE_LIMIT = 10 // docs/13 §3.4's own worked example uses meta.limit = 10.

        fun DeviceOrder.toRowUiModel(): OrderRowUiModel = OrderRowUiModel(
            orderId = orderId,
            label = item,
            // Status (+ courier, when present) folded into plain text,
            // deliberately NOT a second `_status`-suffixed node — see
            // OrderRowUiModel's own kdoc.
            value = buildString {
                append(status.name)
                courier?.let { append(" · $it") }
            },
        )
    }
}
