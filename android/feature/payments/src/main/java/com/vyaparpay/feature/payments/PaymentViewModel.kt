package com.vyaparpay.feature.payments

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.vyaparpay.core.analytics.EventTracker
import com.vyaparpay.core.network.ApiError
import com.vyaparpay.core.network.ApiErrorReporter
import com.vyaparpay.core.network.ApiResult
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

private const val PAISE_PER_RUPEE = 100L

/**
 * Drives `PaymentScreen`'s amount-entry -> Pay Now -> decline-dialog flow —
 * the app-side half of the canonical Rajesh Kumar / ₹245 / `Amazon Business`
 * / `DAILY_LIMIT_EXCEEDED` incident (docs/01-product-and-use-case.md §7
 * steps 1-2).
 *
 * [payments] is the [PaymentRepository] seam (docs/03-android-architecture.md
 * §2.1): today it defaults to [SeededPaymentRepository], the same daily-limit
 * fixture `backend/scripts/seed.py` seeds server-side. Swapping in a
 * Retrofit-backed implementation once `VyaparApi` grows a `/payments`
 * endpoint is a constructor-argument change — the state machine below does
 * not change shape.
 *
 * Not a `@HiltViewModel`: `:feature:payments` carries no Hilt dependency yet,
 * and the only bindings this class would need — [PaymentRepository] and
 * [EventTracker] — have no Hilt module anywhere in the app to provide them
 * (`:app`'s graph binds no `VyaparApi`, and `:core:analytics`'s `EventTracker`
 * singleton is explicitly future work). Constructor defaults keep this class
 * usable with zero DI wiring today; adding `@HiltViewModel` + `@Inject` once
 * real bindings exist is additive, not a rewrite.
 */
public class PaymentViewModel @JvmOverloads constructor(
    private val payments: PaymentRepository = SeededPaymentRepository(),
    public val events: EventTracker = InMemoryEventTracker(),
) : ViewModel() {

    private val errorReporter = ApiErrorReporter(events)

    private val _state = MutableStateFlow(PaymentUiState())
    public val state: StateFlow<PaymentUiState> = _state.asStateFlow()

    /** The amount field changed; digits only, matching integer-rupee input. */
    public fun onAmountChanged(raw: String) {
        val sanitized = raw.filter(Char::isDigit)
        _state.update { it.copy(amountInput = sanitized, amountError = null) }
    }

    /** The decline dialog's dismiss action. */
    public fun onDeclineDialogDismissed() {
        _state.update { it.copy(declineDialog = null) }
    }

    /** The snackbar finished showing (or was dismissed). */
    public fun onSnackbarShown() {
        _state.update { it.copy(snackbar = null) }
    }

    /**
     * Pay Now tapped (docs/01 §7 step 1 — the tap itself is recorded on the
     * timeline by `trackedClickable`, not here).
     */
    public fun onPayNowClicked() {
        val amountPaise = _state.value.amountInput.toLongOrNull()?.times(PAISE_PER_RUPEE)
        if (amountPaise == null || amountPaise <= 0) {
            _state.update { it.copy(amountError = "Enter an amount to pay") }
            return
        }

        _state.update { it.copy(isSubmitting = true, amountError = null) }
        viewModelScope.launch {
            when (val result = payments.payVendor(amountPaise, _state.value.recipient)) {
                is ApiResult.Success -> onPaymentApproved(result.data)
                is ApiResult.Failure -> onPaymentDeclined(result)
            }
        }
    }

    private fun onPaymentApproved(receipt: PaymentReceipt) {
        _state.update {
            it.copy(
                isSubmitting = false,
                amountInput = "",
                snackbar = PaymentSnackbarState(
                    id = System.nanoTime(),
                    message = "Payment of ₹${receipt.amountPaise / PAISE_PER_RUPEE} sent to ${receipt.recipient}",
                ),
            )
        }
    }

    private fun onPaymentDeclined(failure: ApiResult.Failure) {
        // docs/01 §7 step 2: the dialog's appearance is what UiTreeCollector
        // captures alongside the `api_error` timeline entry this records.
        errorReporter.report(failure.code, PaymentDestination.ROUTE)
        _state.update {
            it.copy(
                isSubmitting = false,
                declineDialog = failure.code.toDeclineDialog(failure.message),
                snackbar = PaymentSnackbarState(id = System.nanoTime(), message = "Payment failed"),
            )
        }
    }

    private companion object {
        fun ApiError.toDeclineDialog(fallbackMessage: String): PaymentDeclineDialogState = when (this) {
            ApiError.DAILY_LIMIT_EXCEEDED -> PaymentDeclineDialogState(
                code = this,
                title = "Daily Limit Exceeded",
                message = fallbackMessage,
            )
            else -> PaymentDeclineDialogState(
                code = this,
                title = "Payment Failed",
                message = fallbackMessage,
            )
        }
    }
}
