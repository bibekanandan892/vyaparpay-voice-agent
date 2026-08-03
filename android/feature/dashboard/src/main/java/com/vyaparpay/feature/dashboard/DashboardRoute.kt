package com.vyaparpay.feature.dashboard

import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import com.vyaparpay.core.ui.component.PlaceholderScreen

/**
 * Entry point `:app` navigates to. The seeded dashboard content and its
 * `DashboardViewModel` land with the demo screens (docs/03 §4).
 */
@Composable
public fun DashboardRoute(modifier: Modifier = Modifier) {
    PlaceholderScreen(
        title = "Dashboard",
        screenTestTag = DashboardDestination.ROOT_TEST_TAG,
        modifier = modifier,
    )
}
