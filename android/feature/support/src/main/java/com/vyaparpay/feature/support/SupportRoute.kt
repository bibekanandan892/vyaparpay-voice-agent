package com.vyaparpay.feature.support

import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import com.vyaparpay.core.ui.component.PlaceholderScreen

/**
 * Entry point `:app` navigates to. `SupportButton`, `PermissionManager`,
 * `CallViewModel` and `ConversationOverlay` land with the call flow
 * (docs/03 §3.1, §3.6, §3.12).
 */
@Composable
public fun SupportRoute(modifier: Modifier = Modifier) {
    PlaceholderScreen(
        title = "Help & support",
        screenTestTag = SupportDestination.ROOT_TEST_TAG,
        modifier = modifier,
    )
}
