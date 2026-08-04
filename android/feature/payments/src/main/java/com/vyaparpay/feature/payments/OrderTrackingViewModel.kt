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
 * Drives `OrderTrackingScreen` — loads every order once on construction, then
 * tracks whichever one [selectOrder] was called with.
 *
 * **Review fix.** The original version had no way to be told which order to
 * track at all — `AppNavHost` composed `OrderTrackingRoute` with a bare,
 * argument-free route string, so this always showed the first `IN_TRANSIT`
 * order regardless of which `device_order_row_$index` the user actually
 * tapped. A `{orderId}` NavHost path-segment argument was considered and
 * rejected: `NavigationTracker.ROUTE_TO_FLOW` (`:core:screencontext`, a
 * separately-owned, already-reviewed-and-merged file) matches
 * `NavBackStackEntry.destination.route` by exact string against the literal
 * `"OrderTrackingScreen"` — changing the registered route template to
 * `"OrderTrackingScreen/{orderId}"` would silently break that lookup
 * (`ROUTE_TO_FLOW["OrderTrackingScreen/{orderId}"]` doesn't exist), and that
 * file is out of this task's file-ownership list. Instead, `AppNavHost`
 * holds the tapped order id as plain Compose state and passes it to
 * [OrderTrackingRoute] as a parameter (not a nav argument), which calls
 * [selectOrder] via a `LaunchedEffect` — the route string itself, and
 * `NavigationTracker`'s matching, are untouched.
 *
 * [selectOrder] is safe to call before the initial load resolves: the
 * requested id is retained and applied once loading finishes if the load
 * hasn't completed yet, so there is no race between "screen composed" and
 * "orders loaded" regardless of which happens first.
 */
public class OrderTrackingViewModel @JvmOverloads constructor(
    private val orders: OrdersRepository = SeededOrdersRepository(),
    public val events: EventTracker = InMemoryEventTracker(),
) : ViewModel() {

    private val _state = MutableStateFlow(OrderTrackingUiState())
    public val state: StateFlow<OrderTrackingUiState> = _state.asStateFlow()

    private var loadedOrders: List<DeviceOrder> = emptyList()
    private var requestedOrderId: String? = null

    init {
        viewModelScope.launch {
            when (val result = orders.getOrders(limit = PAGE_LIMIT)) {
                is ApiResult.Success -> {
                    loadedOrders = result.data.items
                    applySelection()
                }
                is ApiResult.Failure -> _state.update { it.copy(isLoading = false) }
            }
        }
    }

    /**
     * @param orderId the id of the `device_order_row_$index` (or
     *   `track_order_cta`'s first-order fallback) the user actually tapped
     *   on `OrdersScreen` — see [OrderTrackingRoute]'s `LaunchedEffect`.
     */
    public fun selectOrder(orderId: String) {
        requestedOrderId = orderId
        if (loadedOrders.isNotEmpty()) applySelection()
    }

    private fun applySelection() {
        val tracked = loadedOrders.firstOrNull { it.orderId == requestedOrderId }
            ?: loadedOrders.firstOrNull { it.status == DeviceOrderStatus.IN_TRANSIT }
            ?: loadedOrders.firstOrNull()
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
