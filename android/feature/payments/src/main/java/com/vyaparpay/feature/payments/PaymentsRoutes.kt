package com.vyaparpay.feature.payments

import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel

/**
 * Entry points `:app` navigates to. `PaymentRoute` is the vendor-payment flow
 * — including the `DAILY_LIMIT_EXCEEDED` path the agent is demonstrated on
 * (docs/03 §4, docs/01 §7-§8); its real content lives in
 * [PaymentScreen]/[PaymentViewModel]. `SettlementsRoute` (settlement batches),
 * `OrdersRoute` (device orders) and `OrderTrackingRoute` (one order's
 * tracking detail — shares the `device_orders` flow group with `OrdersRoute`,
 * docs/03 §3.8) follow the identical state-down/events-up seam.
 */
@Composable
public fun PaymentRoute(
    modifier: Modifier = Modifier,
    viewModel: PaymentViewModel = viewModel(),
) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    PaymentScreen(
        state = state,
        events = viewModel.events,
        onAmountChanged = viewModel::onAmountChanged,
        onPayNowClicked = viewModel::onPayNowClicked,
        onDismissDialog = viewModel::onDeclineDialogDismissed,
        onSnackbarShown = viewModel::onSnackbarShown,
        modifier = modifier,
    )
}

@Composable
public fun SettlementsRoute(
    modifier: Modifier = Modifier,
    viewModel: SettlementsViewModel = viewModel(),
) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    SettlementsScreen(
        state = state,
        events = viewModel.events,
        onExportCsvClicked = viewModel::onExportCsvClicked,
        modifier = modifier,
    )
}

/**
 * @param onOrderSelected fired both when a `device_order_row_$index` is
 *   tapped (with that row's order id) and when `track_order_cta` is tapped
 *   (with the tracked/first order's id) — `:app` wires this to a navigation
 *   call into [OrderTrackingRoute]. See [OrderTrackingViewModel]'s kdoc for
 *   why the id itself is not yet threaded any further than that nav call.
 */
@Composable
public fun OrdersRoute(
    onOrderSelected: (String) -> Unit,
    modifier: Modifier = Modifier,
    viewModel: OrdersViewModel = viewModel(),
) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    OrdersScreen(
        state = state,
        events = viewModel.events,
        onOrderRowClicked = onOrderSelected,
        onTrackOrderClicked = {
            state.rows.firstOrNull()?.let { onOrderSelected(it.orderId) }
        },
        modifier = modifier,
    )
}

@Composable
public fun OrderTrackingRoute(
    onBack: () -> Unit,
    modifier: Modifier = Modifier,
    viewModel: OrderTrackingViewModel = viewModel(),
) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    OrderTrackingScreen(
        state = state,
        events = viewModel.events,
        onBackClicked = onBack,
        modifier = modifier,
    )
}
