package com.vyaparpay.feature.payments

import com.vyaparpay.core.network.ApiError

/**
 * [PaymentViewModel]'s single state object (docs/03-android-architecture.md
 * §2: one `StateFlow<UiState>` per screen, unidirectional data flow).
 *
 * Each field maps onto one of the docs/01-product-and-use-case.md §5
 * canonical `PaymentScreen` roles: [amountInput] -> `amount_field`,
 * [recipient] -> `recipient`, the Pay Now CTA rendered from
 * [isSubmitting]/[canSubmit] -> `primary_cta`, [declineDialog] -> `dialog`,
 * [snackbar] -> `snackbar`.
 */
public data class PaymentUiState(
    public val recipient: String = SeededPaymentRepository.CANONICAL_RECIPIENT,
    public val amountInput: String = "",
    public val amountError: String? = null,
    public val isSubmitting: Boolean = false,
    public val declineDialog: PaymentDeclineDialogState? = null,
    public val snackbar: PaymentSnackbarState? = null,
) {
    /** Whether Pay Now should accept a tap right now. */
    public val canSubmit: Boolean get() = !isSubmitting && amountInput.isNotBlank()
}

/**
 * The decline dialog's content — the screen's `dialog` role node
 * (docs/07-ui-semantic-context.md §4; `protocol/schemas/screen_context.v1.json`
 * `dialog_component`: detected structurally via Compose's `IsDialog`
 * semantics property, which Material3's `AlertDialog` sets on its own, not
 * via a testTag convention).
 */
public data class PaymentDeclineDialogState(
    public val code: ApiError,
    public val title: String,
    public val message: String,
)

/**
 * One transient snackbar message. [id] changes on every emission (including
 * repeated identical failures) so the UI layer can key a `LaunchedEffect` on
 * it and re-show the same text twice in a row.
 */
public data class PaymentSnackbarState(
    public val id: Long,
    public val message: String,
)
