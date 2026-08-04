package com.vyaparpay.feature.payments

import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import com.vyaparpay.core.ui.component.PlaceholderScreen

/**
 * Entry points `:app` navigates to. `PaymentRoute` is the vendor-payment flow
 * — including the `DAILY_LIMIT_EXCEEDED` path the agent is demonstrated on
 * (docs/03 §4, docs/01 §7-§8); its real content lives in
 * [PaymentScreen]/[PaymentViewModel]. `SettlementsRoute`/`OrdersRoute` stay on
 * [PlaceholderScreen] until their own screens land.
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
public fun SettlementsRoute(modifier: Modifier = Modifier) {
    PlaceholderScreen(
        title = "Settlements",
        screenTestTag = SettlementsDestination.ROOT_TEST_TAG,
        modifier = modifier,
    )
}

@Composable
public fun OrdersRoute(modifier: Modifier = Modifier) {
    PlaceholderScreen(
        title = "Device orders",
        screenTestTag = OrdersDestination.ROOT_TEST_TAG,
        modifier = modifier,
    )
}
