package com.vyaparpay.feature.dashboard

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.role
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import com.vyaparpay.core.analytics.EventTracker
import com.vyaparpay.core.ui.modifier.trackedClickable

/** testTag for the wallet balance row -- role `balance_display` (`_balance`). */
private const val WALLET_BALANCE_TEST_TAG = "wallet_balance"

/** testTag for today's collections row -- role `balance_display` (`_balance`). */
private const val COLLECTIONS_TODAY_TEST_TAG = "collections_today_balance"

/** testTag for the next-settlement row -- role `status_badge` (`_status`). */
private const val SETTLEMENT_ETA_TEST_TAG = "settlement_eta_status"

/** testTag for the KYC alert -- role `alert_banner` (`_alert`); docs/03 §4's own worked example. */
private const val KYC_ALERT_TEST_TAG = "kyc_alert"

/** testTag for the screen's one primary CTA -- role `primary_cta` (`_cta`). */
internal const val PAY_VENDOR_TEST_TAG = "pay_vendor_cta"

/** testTag for the "View settlements" quick action -- role `secondary_cta` (`_secondary`). */
internal const val VIEW_SETTLEMENTS_TEST_TAG = "view_settlements_secondary"

/** testTag for the "Device orders" quick action -- role `secondary_cta` (`_secondary`). */
internal const val DEVICE_ORDERS_TEST_TAG = "device_orders_secondary"

/**
 * The real content behind [DashboardRoute] -- the merchant home screen
 * (docs/03-android-architecture.md §4: "Wallet balance (₹18,450), today's
 * collections, quick actions, alert banners"). Split from [DashboardRoute]
 * for the same reason `PaymentScreen` is: Compose UI tests drive it directly
 * against a hand-built [DashboardUiState].
 */
@Composable
internal fun DashboardScreen(
    state: DashboardUiState,
    events: EventTracker,
    onPayVendor: () -> Unit,
    onViewSettlements: () -> Unit,
    onViewOrders: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Box(
        modifier = modifier
            .fillMaxSize()
            .testTag(DashboardDestination.ROOT_TEST_TAG),
    ) {
        Scaffold { padding ->
            DashboardContent(
                state = state,
                events = events,
                onPayVendor = onPayVendor,
                onViewSettlements = onViewSettlements,
                onViewOrders = onViewOrders,
                modifier = Modifier.padding(padding),
            )
        }
    }
}

@Composable
private fun DashboardContent(
    state: DashboardUiState,
    events: EventTracker,
    onPayVendor: () -> Unit,
    onViewSettlements: () -> Unit,
    onViewOrders: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(
        modifier = modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Text(text = "Dashboard", style = MaterialTheme.typography.headlineSmall)

        if (state.isLoading) {
            CircularProgressIndicator(modifier = Modifier.align(Alignment.CenterHorizontally))
        }

        BalanceRow(
            testTag = WALLET_BALANCE_TEST_TAG,
            label = "Wallet balance",
            value = formatRupees(state.walletBalancePaise),
        )
        BalanceRow(
            testTag = COLLECTIONS_TODAY_TEST_TAG,
            label = "Today's collections",
            value = formatRupees(state.collectionsTodayPaise),
        )
        StatusRow(
            testTag = SETTLEMENT_ETA_TEST_TAG,
            label = "Next settlement",
            value = state.settlementEtaLabel,
        )

        val alert = state.alertMessage
        if (alert != null) {
            AlertBanner(testTag = KYC_ALERT_TEST_TAG, message = alert)
        }

        Text(text = "Quick actions", style = MaterialTheme.typography.titleMedium)

        // Gated on isLoading, not a business rule of its own: there is
        // nothing useful to pay against or navigate from before the first
        // load completes. This is also what gives the disabled branch below
        // something real to exercise, per the T4.3 review-fix rationale.
        val actionsEnabled = !state.isLoading

        DashboardCta(
            label = "Pay a vendor",
            testTag = PAY_VENDOR_TEST_TAG,
            enabled = actionsEnabled,
            filled = true,
            events = events,
            onClick = onPayVendor,
            modifier = Modifier.fillMaxWidth(),
        )
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            DashboardCta(
                label = "Settlements",
                testTag = VIEW_SETTLEMENTS_TEST_TAG,
                enabled = actionsEnabled,
                filled = false,
                events = events,
                onClick = onViewSettlements,
                modifier = Modifier.weight(1f),
            )
            DashboardCta(
                label = "Device orders",
                testTag = DEVICE_ORDERS_TEST_TAG,
                enabled = actionsEnabled,
                filled = false,
                events = events,
                onClick = onViewOrders,
                modifier = Modifier.weight(1f),
            )
        }
    }
}

/**
 * A `balance_display` row: exactly two text runs, label first, value second
 * -- the shape `SemanticSnapshotBuilder`'s label/value merge rule (docs/07 §5
 * rule 2) expects, matching `PaymentScreen`'s `RecipientRow`.
 */
@Composable
private fun BalanceRow(testTag: String, label: String, value: String, modifier: Modifier = Modifier) {
    Row(
        modifier = modifier
            .fillMaxWidth()
            .testTag(testTag),
        horizontalArrangement = Arrangement.SpaceBetween,
    ) {
        Text(text = label, style = MaterialTheme.typography.bodyMedium)
        Text(text = value, style = MaterialTheme.typography.headlineSmall)
    }
}

/** A `status_badge` row: same two-run label/value shape as [BalanceRow]. */
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

@Composable
private fun AlertBanner(testTag: String, message: String, modifier: Modifier = Modifier) {
    Surface(
        modifier = modifier
            .fillMaxWidth()
            .testTag(testTag),
        color = MaterialTheme.colorScheme.errorContainer,
        contentColor = MaterialTheme.colorScheme.onErrorContainer,
        shape = MaterialTheme.shapes.small,
    ) {
        Text(
            text = message,
            style = MaterialTheme.typography.bodyMedium,
            modifier = Modifier.padding(12.dp),
        )
    }
}

/**
 * A dashboard quick-action, built on [Surface] + [Modifier.trackedClickable]
 * rather than Material3's `Button`/`OutlinedButton` -- the same reason
 * `PaymentScreen`'s `PayNowButton` is (see its kdoc): stacking
 * `trackedClickable` on top of a `Button`'s own click handling would
 * register two independent clickable regions on one node. Both the enabled
 * and disabled branches attach [Role.Button] explicitly (the T4.3 review fix
 * `PayNowButton` documents) -- disabled here means "loading," not "absent."
 */
@Composable
private fun DashboardCta(
    label: String,
    testTag: String,
    enabled: Boolean,
    filled: Boolean,
    events: EventTracker,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val clickableModifier = if (enabled) {
        Modifier.trackedClickable(
            events = events,
            screen = DashboardDestination.ROUTE,
            testTag = testTag,
            role = Role.Button,
            onClick = onClick,
        )
    } else {
        Modifier
            .testTag(testTag)
            .semantics { role = Role.Button }
    }

    val containerColor = when {
        !enabled -> MaterialTheme.colorScheme.onSurface.copy(alpha = 0.12f)
        filled -> MaterialTheme.colorScheme.primary
        else -> MaterialTheme.colorScheme.secondaryContainer
    }
    val contentColor = when {
        !enabled -> MaterialTheme.colorScheme.onSurface.copy(alpha = 0.38f)
        filled -> MaterialTheme.colorScheme.onPrimary
        else -> MaterialTheme.colorScheme.onSecondaryContainer
    }

    Surface(
        modifier = modifier
            .height(48.dp)
            .then(clickableModifier),
        shape = MaterialTheme.shapes.small,
        color = containerColor,
        contentColor = contentColor,
    ) {
        Box(contentAlignment = Alignment.Center, modifier = Modifier.fillMaxSize()) {
            Text(text = label, style = MaterialTheme.typography.labelLarge)
        }
    }
}
