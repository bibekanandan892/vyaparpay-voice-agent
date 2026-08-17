package com.vyaparpay

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Call
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.unit.dp
import com.vyaparpay.core.analytics.EventTracker
import com.vyaparpay.core.ui.modifier.trackedClickable
import com.vyaparpay.navigation.AppRoute

/** testTag for the return-to-call chip. */
internal const val RETURN_TO_CALL_CHIP_TEST_TAG = "return_to_call_chip"

/**
 * A plain function, not inlined into [MainActivity]'s composition, so the
 * visibility rule is unit-testable with no Compose/Hilt infrastructure at
 * all. Hidden on [AppRoute.SUPPORT] specifically because that route already
 * renders the full call screen ([com.vyaparpay.feature.support.CallScreen])
 * — a chip back to a screen already showing the call would be a second,
 * redundant way to reach the same place.
 */
internal fun shouldShowReturnToCallChip(isCallLive: Boolean, currentRoute: String?): Boolean =
    isCallLive && currentRoute != AppRoute.SUPPORT.route

/**
 * The affordance [com.vyaparpay.feature.support.SupportButton]'s own kdoc
 * names as a deliberate deferral: a way back into a live call from anywhere
 * else in the app, hosted in [MainActivity] because that composition root is
 * the one thing above `AppNavHost` that survives navigation (that kdoc's
 * judgment call 3) — the exact placement gap the FAB kdoc describes, closed
 * here for the call case rather than the general one.
 *
 * Deliberately "dumb": whether to show this at all — is a call live, is the
 * merchant already on the one screen where a chip back to it would be
 * redundant — is [MainActivity]'s call to make by composing this in or out,
 * the same division of labour [CallSurface] already settled between
 * `CallScreen`/`CallStatusPanel` and whichever caller decides which one
 * belongs on screen.
 */
@Composable
internal fun ReturnToCallChip(
    events: EventTracker,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Surface(
        shape = RoundedCornerShape(24.dp),
        color = MaterialTheme.colorScheme.primary,
        contentColor = MaterialTheme.colorScheme.onPrimary,
        tonalElevation = 4.dp,
        shadowElevation = 4.dp,
        modifier = modifier.trackedClickable(
            events = events,
            // Cross-cutting, not owned by any one feature's screen — "app"
            // names that honestly rather than attributing the tap to
            // whichever screen the chip happened to float over.
            screen = "app",
            testTag = RETURN_TO_CALL_CHIP_TEST_TAG,
            role = Role.Button,
            onClick = onClick,
        ),
    ) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            modifier = Modifier.padding(horizontal = 16.dp, vertical = 10.dp),
        ) {
            Icon(imageVector = Icons.Filled.Call, contentDescription = null)
            Text(text = "Return to call", style = MaterialTheme.typography.labelLarge)
        }
    }
}
