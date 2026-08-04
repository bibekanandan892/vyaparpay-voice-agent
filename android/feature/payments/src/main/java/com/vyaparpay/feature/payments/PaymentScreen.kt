package com.vyaparpay.feature.payments

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Snackbar
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import com.vyaparpay.core.analytics.EventTracker
import com.vyaparpay.core.ui.modifier.trackedClickable
import kotlinx.coroutines.delay

/** How long a transient snackbar stays up before [PaymentViewModel.onSnackbarShown] clears it. */
private const val SNACKBAR_AUTO_DISMISS_MS = 3_000L

/** testTag for the transient confirmation/error surface — role `snackbar` (docs/03 §4). */
private const val SNACKBAR_TEST_TAG = "payment_snackbar"

/** testTag for the amount field — role `amount_field` (docs/03 §4's own worked example for this screen). */
private const val AMOUNT_TEST_TAG = "amount_input"

/** testTag for the recipient row — role `recipient` (docs/03 §4's own worked example for this screen). */
private const val RECIPIENT_TEST_TAG = "recipient_row"

/** testTag for the primary CTA — role `primary_cta` (docs/03 §4's own worked example for this screen). */
internal const val PAY_NOW_TEST_TAG = "pay_now_cta"

/** testTag for the decline dialog's dismiss action. */
private const val DIALOG_DISMISS_TEST_TAG = "daily_limit_dismiss_secondary"

/**
 * The real content behind [PaymentRoute], split out so Compose UI tests can
 * drive it directly against a hand-built [PaymentUiState] without a live
 * [PaymentViewModel] — the state-down/events-up seam
 * docs/03-android-architecture.md §2 describes.
 *
 * The five required nodes map onto the docs/01-product-and-use-case.md §5
 * canonical `PaymentScreen` roles: the amount field (`amount_field`), the
 * recipient row (`recipient`), the Pay Now CTA (`primary_cta`), the decline
 * [AlertDialog] (`dialog` — detected structurally via Compose's `IsDialog`
 * semantics property, not a testTag; see `protocol/schemas/screen_context.v1.json`
 * `dialog_component`), and the [Snackbar] (`snackbar`).
 */
@Composable
internal fun PaymentScreen(
    state: PaymentUiState,
    events: EventTracker,
    onAmountChanged: (String) -> Unit,
    onPayNowClicked: () -> Unit,
    onDismissDialog: () -> Unit,
    onSnackbarShown: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Box(
        modifier = modifier
            .fillMaxSize()
            .testTag(PaymentDestination.ROOT_TEST_TAG),
    ) {
        Scaffold { padding ->
            PaymentContent(
                state = state,
                events = events,
                onAmountChanged = onAmountChanged,
                onPayNowClicked = onPayNowClicked,
                modifier = Modifier.padding(padding),
            )
        }

        val snackbar = state.snackbar
        if (snackbar != null) {
            LaunchedEffect(snackbar.id) {
                delay(SNACKBAR_AUTO_DISMISS_MS)
                onSnackbarShown()
            }
            Snackbar(
                modifier = Modifier
                    .align(Alignment.BottomCenter)
                    .padding(16.dp)
                    .testTag(SNACKBAR_TEST_TAG),
            ) {
                Text(snackbar.message)
            }
        }

        val dialog = state.declineDialog
        if (dialog != null) {
            AlertDialog(
                onDismissRequest = onDismissDialog,
                title = { Text(dialog.title) },
                text = { Text(dialog.message) },
                confirmButton = {
                    TextButton(
                        onClick = onDismissDialog,
                        modifier = Modifier.testTag(DIALOG_DISMISS_TEST_TAG),
                    ) { Text("Got it") }
                },
            )
        }
    }
}

@Composable
private fun PaymentContent(
    state: PaymentUiState,
    events: EventTracker,
    onAmountChanged: (String) -> Unit,
    onPayNowClicked: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(
        modifier = modifier
            .fillMaxSize()
            .padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Text(text = "Pay a vendor", style = MaterialTheme.typography.headlineSmall)

        RecipientRow(recipient = state.recipient)

        OutlinedTextField(
            value = state.amountInput,
            onValueChange = onAmountChanged,
            modifier = Modifier
                .fillMaxWidth()
                .testTag(AMOUNT_TEST_TAG),
            label = { Text("Amount") },
            prefix = { Text("₹") },
            singleLine = true,
            isError = state.amountError != null,
            supportingText = state.amountError?.let { message -> { Text(message) } },
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
        )

        PayNowButton(
            enabled = state.canSubmit,
            isSubmitting = state.isSubmitting,
            events = events,
            onClick = onPayNowClicked,
        )
    }
}

@Composable
private fun RecipientRow(recipient: String, modifier: Modifier = Modifier) {
    Row(
        modifier = modifier
            .fillMaxWidth()
            .testTag(RECIPIENT_TEST_TAG),
        horizontalArrangement = Arrangement.SpaceBetween,
    ) {
        Text(text = "Paying", style = MaterialTheme.typography.bodyMedium)
        Text(text = recipient, style = MaterialTheme.typography.bodyLarge)
    }
}

/**
 * The Pay Now CTA, built on [Surface] rather than Material3's `Button` so its
 * click handling is [Modifier.trackedClickable] alone — stacking
 * `trackedClickable` on top of `Button`'s own built-in click handling would
 * register two independent clickable regions on one node. [enabled] gates
 * whether `trackedClickable` is attached at all, since the helper carries no
 * enabled/disabled concept of its own.
 */
@Composable
private fun PayNowButton(
    enabled: Boolean,
    isSubmitting: Boolean,
    events: EventTracker,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val clickableModifier = if (enabled) {
        Modifier.trackedClickable(
            events = events,
            screen = PaymentDestination.ROUTE,
            testTag = PAY_NOW_TEST_TAG,
            onClick = onClick,
        )
    } else {
        Modifier.testTag(PAY_NOW_TEST_TAG)
    }

    Surface(
        modifier = modifier
            .fillMaxWidth()
            .height(48.dp)
            .then(clickableModifier),
        shape = MaterialTheme.shapes.small,
        color = if (enabled) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.onSurface.copy(alpha = 0.12f),
        contentColor = if (enabled) MaterialTheme.colorScheme.onPrimary else MaterialTheme.colorScheme.onSurface.copy(alpha = 0.38f),
    ) {
        Box(contentAlignment = Alignment.Center, modifier = Modifier.fillMaxSize()) {
            if (isSubmitting) {
                CircularProgressIndicator(
                    modifier = Modifier.size(20.dp),
                    strokeWidth = 2.dp,
                    color = LocalContentColor.current,
                )
            } else {
                Text(text = "Pay Now", style = MaterialTheme.typography.labelLarge)
            }
        }
    }
}
