package com.vyaparpay.feature.support

import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel

/**
 * Entry point `:app` navigates to. `HelpScreen`'s FAQ hub and complaint
 * history live here; `SupportButton`, `PermissionManager`, `CallViewModel`
 * and `ConversationOverlay` remain future work landing with the call flow
 * (docs/03 §3.1, §3.6, §3.12) -- see [HelpScreen]'s kdoc for how the inline
 * "Call Support" CTA reconciles with that. [onCallSupport] defaults to a
 * no-op (matching `PaymentScreen`'s nav-callback convention) so `:app`'s
 * `AppNavHost` keeps compiling unchanged against this signature.
 */
@Composable
public fun SupportRoute(
    modifier: Modifier = Modifier,
    viewModel: HelpViewModel = viewModel(),
    onCallSupport: () -> Unit = {},
) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    HelpScreen(
        state = state,
        events = viewModel.events,
        onCallSupport = onCallSupport,
        modifier = modifier,
    )
}
