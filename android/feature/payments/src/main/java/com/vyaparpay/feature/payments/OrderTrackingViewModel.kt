package com.vyaparpay.feature.payments

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.vyaparpay.core.analytics.EventTracker
import com.vyaparpay.core.network.ApiResult
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

private val MONTH_ABBREVIATIONS = arrayOf(
    "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)

/**
 * [OrderTrackingViewModel]'s single state object (docs/03-android-architecture.md
 * §2). No standalone file for this one — the file list this task was scoped
 * against names only `OrdersUiState.kt` for the Orders/OrderTracking pair, so
 * this state lives next to the `ViewModel` that owns it, same as
 * `PaymentDeclineDialogState`/`PaymentSnackbarState` living in
 * `PaymentUiState.kt` rather than each getting its own file.
 *
 * [statusValue] -> `tracker_state_status`, [etaValue] -> `order_eta_status`
 * (both `status_badge`, docs/03 §4).
 */
public data class OrderTrackingUiState(
    public val isLoading: Boolean = true,
    public val statusValue: String = "",
    public val etaValue: String = "",
)

/**
 * Drives `OrderTrackingScreen` — loads the tracked order once on
 * construction.
 *
 * `OrderTrackingScreen` carries no nav-arg route today: docs/03 §3.8's route
 * table gives it none, and no `NavType` is declared anywhere in this app yet
 * (`AppNavHost` composes every destination with a bare, argument-free route
 * string). So this screen always tracks the same order this demo's
 * device-orders flow revolves around — the first `IN_TRANSIT` order in
 * [OrdersRepository] (docs/13 §3.4's canonical soundbox) — regardless of
 * which `device_order_row_$index` the user tapped on `OrdersScreen`. Threading
 * a real order id through the nav graph (a `{orderId}` path segment and a
 * matching `NavType`) is a real follow-up once more than one order needs its
 * own tracking view; out of scope for this task's closed file list, which
 * gives `OrderTrackingRoute` an `onBack` callback only.
 */
public class OrderTrackingViewModel @JvmOverloads constructor(
    private val orders: OrdersRepository = SeededOrdersRepository(),
    public val events: EventTracker = InMemoryEventTracker(),
) : ViewModel() {

    private val _state = MutableStateFlow(OrderTrackingUiState())
    public val state: StateFlow<OrderTrackingUiState> = _state.asStateFlow()

    init {
        viewModelScope.launch {
            when (val result = orders.getOrders(limit = PAGE_LIMIT)) {
                is ApiResult.Success -> onLoaded(result.data)
                is ApiResult.Failure -> _state.update { it.copy(isLoading = false) }
            }
        }
    }

    private fun onLoaded(page: OrdersPage) {
        val tracked = page.items.firstOrNull { it.status == DeviceOrderStatus.IN_TRANSIT } ?: page.items.firstOrNull()
        _state.update {
            it.copy(
                isLoading = false,
                statusValue = tracked?.status?.toDisplayLabel().orEmpty(),
                etaValue = tracked?.etaDate?.let(::formatDateLabel).orEmpty(),
            )
        }
    }

    private companion object {
        const val PAGE_LIMIT = 10

        fun DeviceOrderStatus.toDisplayLabel(): String = when (this) {
            DeviceOrderStatus.PLACED -> "Placed"
            DeviceOrderStatus.PACKED -> "Packed"
            DeviceOrderStatus.IN_TRANSIT -> "In transit"
            DeviceOrderStatus.DELIVERED -> "Delivered"
            DeviceOrderStatus.RETURNED -> "Returned"
        }

        fun formatDateLabel(isoDate: String): String {
            val (year, month, day) = isoDate.split("-")
            return "${day.toInt()} ${MONTH_ABBREVIATIONS[month.toInt() - 1]} $year"
        }
    }
}
