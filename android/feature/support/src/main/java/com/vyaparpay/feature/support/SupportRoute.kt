package com.vyaparpay.feature.support

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.hilt.lifecycle.viewmodel.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle

/**
 * Entry point `:app` navigates to, and — as of Phase-4 T8c — the place the
 * whole call trigger is assembled.
 *
 * `HelpScreen`'s FAQ hub and complaint history still come from [HelpViewModel];
 * what is new is the chain behind its "Call Support" CTA:
 * [SupportButton] tap -> [rememberCallPermissionGate] -> [CallViewModel] ->
 * `VoiceCallService`. Each link has its own kdoc for the judgment calls it
 * carries.
 *
 * **Why the trigger lives here rather than behind an `onCallSupport` callback
 * hoisted to `:app`.** The previous signature took `onCallSupport: () -> Unit =
 * {}` precisely because nothing could implement it yet. Hoisting it now would
 * put permission handling and service starting in `AppNavHost`, where every
 * other feature's navigation lives — and `:app` cannot even see
 * `VoiceCallService`'s intent contract without becoming the place that knows
 * how a support call works. The `:feature:support -> :voice` edge exists
 * exactly so this module can own that (docs/03 §1 whitelists it as the one
 * feature-to-`:voice` dependency), so the parameter is gone rather than
 * rewired. `:app`'s `AppNavHost` calls `SupportRoute()` with no arguments and
 * is unaffected.
 *
 * **Two ViewModels, deliberately.** [CallViewModel] is not folded into
 * [HelpViewModel]: the call outlives the screen conceptually (it owns a
 * service binding and a foreground service), while [HelpViewModel] is ordinary
 * screen content. Keeping them apart is also what lets `HelpScreenTest` keep
 * driving the screen with a bare lambda and no Hilt graph at all.
 */
@Composable
public fun SupportRoute(
    modifier: Modifier = Modifier,
    viewModel: HelpViewModel = hiltViewModel(),
    callViewModel: CallViewModel = hiltViewModel(),
) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    val callState by callViewModel.state.collectAsStateWithLifecycle()
    val context = LocalContext.current

    val requestCallPermissions = rememberCallPermissionGate(
        onDecision = callViewModel::onPermissionDecision,
    )

    Box(modifier = modifier.fillMaxSize()) {
        HelpScreen(
            state = state,
            events = viewModel.events,
            // Always the permission gate, never startCall directly: RECORD_AUDIO
            // must be granted *before* the foreground service starts, since
            // VoiceCallService is microphone-typed and Android 14+ rejects a
            // mic-typed FGS started without it (docs/03 §3.3).
            onCallSupport = requestCallPermissions,
        )
        CallStatusPanel(
            state = callState,
            // The same EventTracker HelpScreen taps land on, so ending a call
            // is on the agent's timeline alongside the tap that started it.
            events = viewModel.events,
            onHangUp = callViewModel::hangUp,
            onDismiss = callViewModel::dismiss,
            onOpenSettings = { PermissionManager.openAppSettings(context) },
            modifier = Modifier
                .align(Alignment.BottomCenter)
                .padding(16.dp),
        )
    }
}
