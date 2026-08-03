package com.vyaparpay.feature.payments

import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import com.vyaparpay.core.ui.component.PlaceholderScreen

/**
 * Entry points `:app` navigates to. The seeded screens and their ViewModels —
 * including the `DAILY_LIMIT_EXCEEDED` path the agent is demonstrated on — land
 * with the demo screens (docs/03 §4).
 */
@Composable
public fun PaymentRoute(modifier: Modifier = Modifier) {
    PlaceholderScreen(
        title = "Pay a vendor",
        screenTestTag = PaymentDestination.ROOT_TEST_TAG,
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
