package com.vyaparpay.feature.payments

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.CollectionItemInfo
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.collectionItemInfo
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import com.vyaparpay.core.analytics.EventTracker
import com.vyaparpay.core.ui.modifier.trackedClickable

/** testTag for the device-orders `LazyColumn` — role `list`, resolved structurally via `CollectionInfo` (docs/07 §4). */
internal const val ORDERS_LIST_TEST_TAG = "device_orders_list"

/** testTag for the screen's only primary action — role `primary_cta` (docs/03 §4). */
internal const val TRACK_ORDER_TEST_TAG = "track_order_cta"

/**
 * The real content behind [OrdersRoute] — device orders (soundbox, QR kit)
 * with courier tracking state (docs/03 §4).
 *
 * Rows are clickable ([Modifier.trackedClickable], no explicit [Role] — a
 * clickable list row is a navigation target, not a command button, so unlike
 * a `Surface`-based CTA it is left without one) and carry an explicit
 * [CollectionItemInfo] for the same `UiTreeCollector.isListItem` reason
 * `SettlementBatchRow`'s kdoc documents in full.
 */
@Composable
internal fun OrdersScreen(
    state: OrdersUiState,
    events: EventTracker,
    onOrderRowClicked: (String) -> Unit,
    onTrackOrderClicked: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Box(
        modifier = modifier
            .fillMaxSize()
            .testTag(OrdersDestination.ROOT_TEST_TAG),
    ) {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(24.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            Text(text = "Device orders", style = MaterialTheme.typography.headlineSmall)

            LazyColumn(
                modifier = Modifier
                    .weight(1f)
                    .fillMaxWidth()
                    .testTag(ORDERS_LIST_TEST_TAG),
            ) {
                items(count = state.rows.size) { index ->
                    OrderRow(
                        row = state.rows[index],
                        index = index,
                        events = events,
                        onClick = { onOrderRowClicked(state.rows[index].orderId) },
                    )
                }
            }

            TrackOrderButton(events = events, onClick = onTrackOrderClicked)
        }
    }
}

@Composable
private fun OrderRow(
    row: OrderRowUiModel,
    index: Int,
    events: EventTracker,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Row(
        modifier = modifier
            .fillMaxWidth()
            .padding(vertical = 8.dp)
            .semantics {
                collectionItemInfo = CollectionItemInfo(
                    rowIndex = index,
                    rowSpan = 1,
                    columnIndex = 0,
                    columnSpan = 1,
                )
            }
            .trackedClickable(
                events = events,
                screen = OrdersDestination.ROUTE,
                testTag = "device_order_row_$index",
                onClick = onClick,
            ),
        horizontalArrangement = Arrangement.SpaceBetween,
    ) {
        Text(text = row.label, style = MaterialTheme.typography.bodyMedium)
        Text(text = row.value, style = MaterialTheme.typography.bodyMedium)
    }
}

/**
 * Built on [Surface] + [Modifier.trackedClickable] with an explicit
 * [Role.Button] attached unconditionally — the same review-fix discipline
 * `PaymentScreen.PayNowButton`'s kdoc documents (T4.3). No disabled state on
 * this CTA, so one branch is enough.
 */
@Composable
private fun TrackOrderButton(events: EventTracker, onClick: () -> Unit, modifier: Modifier = Modifier) {
    Surface(
        modifier = modifier
            .fillMaxWidth()
            .height(48.dp)
            .trackedClickable(
                events = events,
                screen = OrdersDestination.ROUTE,
                testTag = TRACK_ORDER_TEST_TAG,
                role = Role.Button,
                onClick = onClick,
            ),
        shape = MaterialTheme.shapes.small,
        color = MaterialTheme.colorScheme.primary,
        contentColor = MaterialTheme.colorScheme.onPrimary,
    ) {
        Box(contentAlignment = Alignment.Center, modifier = Modifier.fillMaxSize()) {
            Text(text = "Track Order", style = MaterialTheme.typography.labelLarge)
        }
    }
}
