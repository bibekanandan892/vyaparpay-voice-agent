package com.vyaparpay.feature.dashboard

import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.hilt.lifecycle.viewmodel.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle

/**
 * Entry point `:app` navigates to. The seeded dashboard content lives in
 * [DashboardScreen]/[DashboardViewModel] (docs/03 §4). The nav-callback
 * parameters default to no-ops (matching `PaymentScreen`'s convention) so
 * `:app`'s `AppNavHost` keeps compiling unchanged against this signature
 * until it wires real destinations for each quick action.
 */
@Composable
public fun DashboardRoute(
    modifier: Modifier = Modifier,
    viewModel: DashboardViewModel = hiltViewModel(),
    onPayVendor: () -> Unit = {},
    onViewSettlements: () -> Unit = {},
    onViewOrders: () -> Unit = {},
) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    DashboardScreen(
        state = state,
        events = viewModel.events,
        onPayVendor = onPayVendor,
        onViewSettlements = onViewSettlements,
        onViewOrders = onViewOrders,
        modifier = modifier,
    )
}
