package com.vyaparpay.feature.payments

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.unit.dp
import com.vyaparpay.core.analytics.EventTracker
import com.vyaparpay.core.ui.modifier.trackedClickable

/** testTag for the tracker's current-state row — role `status_badge` (docs/03 §4). */
internal const val TRACKER_STATE_TEST_TAG = "tracker_state_status"

/** testTag for the ETA row — role `status_badge` (docs/03 §4). */
internal const val ORDER_ETA_TEST_TAG = "order_eta_status"

/** testTag for the screen's only secondary action — role `secondary_cta` (docs/03 §4). */
internal const val BACK_TO_ORDERS_TEST_TAG = "back_to_orders_secondary"

/**
 * The real content behind [OrderTrackingRoute] — one device order's tracking
 * detail (docs/03 §4's `OrdersScreen` one-liner covers courier tracking
 * state; this is that state's own screen, sharing the `device_orders` flow
 * group per docs/03 §3.8).
 */
@Composable
internal fun OrderTrackingScreen(
    state: OrderTrackingUiState,
    events: EventTracker,
    onBackClicked: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Box(
        modifier = modifier
            .fillMaxSize()
            .testTag(OrderTrackingDestination.ROOT_TEST_TAG),
    ) {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(24.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            Text(text = "Track order", style = MaterialTheme.typography.headlineSmall)

            StatusRow(testTag = TRACKER_STATE_TEST_TAG, label = "Status", value = state.statusValue)
            StatusRow(testTag = ORDER_ETA_TEST_TAG, label = "Arriving", value = state.etaValue)

            BackToOrdersButton(events = events, onClick = onBackClicked)
        }
    }
}

@Composable
private fun StatusRow(testTag: String, label: String, value: String, modifier: Modifier = Modifier) {
    Row(
        modifier = modifier
            .fillMaxWidth()
            .testTag(testTag),
        horizontalArrangement = Arrangement.SpaceBetween,
    ) {
        Text(text = label, style = MaterialTheme.typography.bodyMedium)
        Text(text = value, style = MaterialTheme.typography.bodyLarge)
    }
}

/**
 * Built on [Surface] + [Modifier.trackedClickable] with an explicit
 * [Role.Button] attached unconditionally — the same review-fix discipline
 * `PaymentScreen.PayNowButton`'s kdoc documents (T4.3).
 */
@Composable
private fun BackToOrdersButton(events: EventTracker, onClick: () -> Unit, modifier: Modifier = Modifier) {
    Surface(
        modifier = modifier
            .fillMaxWidth()
            .height(40.dp)
            .trackedClickable(
                events = events,
                screen = OrderTrackingDestination.ROUTE,
                testTag = BACK_TO_ORDERS_TEST_TAG,
                role = Role.Button,
                onClick = onClick,
            ),
        shape = MaterialTheme.shapes.small,
        color = MaterialTheme.colorScheme.surfaceVariant,
    ) {
        Box(contentAlignment = Alignment.Center, modifier = Modifier.fillMaxSize()) {
            Text(text = "Back to orders", style = MaterialTheme.typography.labelLarge)
        }
    }
}
