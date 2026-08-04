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

/** testTag for the net-settled header figure — role `balance_display` (docs/03 §4). */
private const val NET_BALANCE_TEST_TAG = "settlements_net_balance"

/** testTag for the fee-breakdown header figure — role `balance_display` (docs/03 §4). */
private const val FEES_BALANCE_TEST_TAG = "settlements_fees_balance"

/** testTag for the latest-batch header chip — role `status_badge`, deliberately OUTSIDE the list (see the row-contiguity hazard this task's brief documents). */
private const val LATEST_STATUS_TEST_TAG = "latest_batch_status"

/** testTag for the shortfall notice — role `alert_banner` (docs/03 §4). Rendered only when [SettlementsUiState.shortfallMessage] is non-null. */
private const val SHORTFALL_ALERT_TEST_TAG = "shortfall_alert"

/** testTag for the header's only secondary action — role `secondary_cta` (docs/03 §4). */
internal const val EXPORT_CSV_TEST_TAG = "export_csv_secondary"

/** testTag for the settlement-batches `LazyColumn` — role `list`, resolved structurally via `CollectionInfo`, not this testTag (docs/07 §4). */
internal const val SETTLEMENTS_LIST_TEST_TAG = "settlement_batches_list"

/**
 * The real content behind [SettlementsRoute] — settlement batches with
 * status badges and net/fee breakdown (docs/03 §4).
 *
 * The header carries every `status_badge`/`alert_banner`/`balance_display`
 * node; the list rows carry none of them, per this task's own hard
 * constraint: a per-row status badge would insert a second role-resolvable
 * node between consecutive `list_item`s, breaking the
 * `[list, list_item, list_item, ...]` contiguity
 * `SemanticSnapshotBuilder.fillListVisibleCounts` and
 * `DropLadder.rung1CapListItems` both depend on.
 */
@Composable
internal fun SettlementsScreen(
    state: SettlementsUiState,
    events: EventTracker,
    onExportCsvClicked: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Box(
        modifier = modifier
            .fillMaxSize()
            .testTag(SettlementsDestination.ROOT_TEST_TAG),
    ) {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(24.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            Text(text = "Settlements", style = MaterialTheme.typography.headlineSmall)

            LabelValueRow(testTag = NET_BALANCE_TEST_TAG, label = "Net settled (30d)", value = state.netSettledValue)
            LabelValueRow(testTag = FEES_BALANCE_TEST_TAG, label = "Fees (30d)", value = state.feesValue)
            LabelValueRow(testTag = LATEST_STATUS_TEST_TAG, label = "Latest batch", value = state.latestBatchStatus)

            val shortfall = state.shortfallMessage
            if (shortfall != null) {
                AlertBanner(testTag = SHORTFALL_ALERT_TEST_TAG, message = shortfall)
            }

            ExportCsvButton(events = events, onClick = onExportCsvClicked)

            Text(text = "Settlement batches", style = MaterialTheme.typography.titleMedium)

            LazyColumn(
                modifier = Modifier
                    .weight(1f)
                    .fillMaxWidth()
                    .testTag(SETTLEMENTS_LIST_TEST_TAG),
            ) {
                items(count = state.rows.size) { index ->
                    SettlementBatchRow(row = state.rows[index], index = index)
                }
            }
        }
    }
}

@Composable
private fun LabelValueRow(testTag: String, label: String, value: String, modifier: Modifier = Modifier) {
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

@Composable
private fun AlertBanner(testTag: String, message: String, modifier: Modifier = Modifier) {
    Surface(
        modifier = modifier
            .fillMaxWidth()
            .testTag(testTag),
        shape = MaterialTheme.shapes.small,
        color = MaterialTheme.colorScheme.errorContainer,
    ) {
        Text(
            text = message,
            modifier = Modifier.padding(12.dp),
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onErrorContainer,
        )
    }
}

/**
 * Built on [Surface] + [Modifier.trackedClickable] with an explicit
 * [Role.Button] attached unconditionally — the same discipline
 * `PaymentScreen.PayNowButton`'s kdoc documents as a real review fix (T4.3):
 * `Surface` never infers a `Role` on its own, and omitting one leaves
 * TalkBack announcing a generic clickable region instead of a button. This
 * CTA carries no disabled state (unlike Pay Now), so a single branch is
 * enough — there is no second branch whose `Role` could be forgotten.
 */
@Composable
private fun ExportCsvButton(events: EventTracker, onClick: () -> Unit, modifier: Modifier = Modifier) {
    Surface(
        modifier = modifier
            .fillMaxWidth()
            .height(40.dp)
            .trackedClickable(
                events = events,
                screen = SettlementsDestination.ROUTE,
                testTag = EXPORT_CSV_TEST_TAG,
                role = Role.Button,
                onClick = onClick,
            ),
        shape = MaterialTheme.shapes.small,
        color = MaterialTheme.colorScheme.surfaceVariant,
    ) {
        Box(contentAlignment = Alignment.Center, modifier = Modifier.fillMaxSize()) {
            Text(text = "Export CSV", style = MaterialTheme.typography.labelLarge)
        }
    }
}

/**
 * One settlement-batch row — exactly two [Text] runs ([SettlementRowUiModel.label],
 * [SettlementRowUiModel.value]) and an explicit [CollectionItemInfo], per this
 * task's hard constraint: `LazyColumn` sets `CollectionInfo` on its own
 * container automatically but does NOT set `CollectionItemInfo` on items —
 * without it, `UiTreeCollector`'s `isListItem` check
 * (`config.contains(SemanticsProperties.CollectionItemInfo)`) would not
 * recognize this row as a list item at all. Not clickable — unlike
 * `OrdersScreen`'s rows, nothing in this task's brief asks a settlement batch
 * row to navigate anywhere.
 */
@Composable
private fun SettlementBatchRow(row: SettlementRowUiModel, index: Int, modifier: Modifier = Modifier) {
    Row(
        modifier = modifier
            .fillMaxWidth()
            .padding(vertical = 8.dp)
            .testTag("settlement_batch_row_$index")
            .semantics {
                collectionItemInfo = CollectionItemInfo(
                    rowIndex = index,
                    rowSpan = 1,
                    columnIndex = 0,
                    columnSpan = 1,
                )
            },
        horizontalArrangement = Arrangement.SpaceBetween,
    ) {
        Text(text = row.label, style = MaterialTheme.typography.bodyMedium)
        Text(text = row.value, style = MaterialTheme.typography.bodyMedium)
    }
}
