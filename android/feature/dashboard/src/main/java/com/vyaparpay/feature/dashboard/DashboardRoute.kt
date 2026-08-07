package com.vyaparpay.feature.dashboard

import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.hilt.lifecycle.viewmodel.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle

/**
 * Entry point `:app` navigates to. The seeded dashboard content lives in
 * [DashboardScreen]/[DashboardViewModel] (docs/03 §4). The
 * onPayVendor/onViewSettlements/onViewOrders nav-callback parameters
 * default to no-ops (matching `PaymentScreen`'s convention) so `:app`'s
 * `AppNavHost` keeps compiling unchanged against this signature until it
 * wires real destinations for each quick action.
 *
 * [onNeedHelp] is wired for real, not left as a no-op like its three
 * siblings above — DashboardDestination's own kdoc already declares this
 * "the SupportButton is shown here," and until this call site existed
 * `AppRoute.SUPPORT` ("HelpScreen") was unreachable from the running app:
 * no navigate() call anywhere in :app or any :feature module ever targeted
 * it. This is the minimal fix for that (a "Need help?" quick action,
 * wired in AppNavHost the same way ORDER_TRACKING already is) — not the
 * persistent floating SupportButton docs/03 §3.1's visibility-rules table
 * describes across every captured screen, which is a larger, separate
 * piece of work.
 */
@Composable
public fun DashboardRoute(
    modifier: Modifier = Modifier,
    viewModel: DashboardViewModel = hiltViewModel(),
    onPayVendor: () -> Unit = {},
    onViewSettlements: () -> Unit = {},
    onViewOrders: () -> Unit = {},
    onNeedHelp: () -> Unit = {},
) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    DashboardScreen(
        state = state,
        events = viewModel.events,
        onPayVendor = onPayVendor,
        onViewSettlements = onViewSettlements,
        onViewOrders = onViewOrders,
        onNeedHelp = onNeedHelp,
        modifier = modifier,
    )
}
