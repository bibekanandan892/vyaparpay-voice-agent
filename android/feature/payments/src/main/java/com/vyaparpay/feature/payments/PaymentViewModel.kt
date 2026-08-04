package com.vyaparpay.feature.payments

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.vyaparpay.core.analytics.EventTracker
import com.vyaparpay.core.network.ApiError
import com.vyaparpay.core.network.ApiErrorReporter
import com.vyaparpay.core.network.ApiResult
import dagger.hilt.android.lifecycle.HiltViewModel
import javax.inject.Inject
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
 * **Now `@HiltViewModel` (Phase-4 T8a).** [PaymentRepository] still has no
 * Hilt module anywhere in the app (`:app`'s graph binds no `VyaparApi` yet) —
 * repository seeding is unchanged and explicitly out of this task's scope —
 * but [EventTracker] does now (`AnalyticsModule` binds the real, shared
 * `RingBufferEventTracker`, `:core:analytics`). [events] is the one
 * constructor parameter this task wires to real DI; see the `@Inject`
 * secondary constructor below for why [payments] is deliberately NOT part of
 * it.
 */
@HiltViewModel
public class PaymentViewModel @JvmOverloads constructor(
    private val payments: PaymentRepository = SeededPaymentRepository(),
    public val events: EventTracker = InMemoryEventTracker(),
) : ViewModel() {

    /**
     * **Judgment call — a narrower `@Inject` constructor, not the primary
     * one above.** Dagger/Hilt ignores Kotlin default parameter values on an
     * `@Inject`-annotated constructor: annotating the primary constructor
     * directly would force Hilt to also resolve [PaymentRepository], which
     * has no Hilt binding anywhere in this app (see this class's own kdoc) —
     * that would break the build, not just leave [payments] un-DI'd. This
     * constructor takes only [events] — the one dependency a real Hilt
     * module ([com.vyaparpay.core.analytics.di.AnalyticsModule]) can
     * provide. `PaymentRoute`'s `hiltViewModel()` call resolves to *this*
     * constructor; every existing test that constructs
     * `PaymentViewModel(payments = ...)` directly still resolves to the
     * primary constructor unambiguously (different parameter name/type), so
     * none of them needed to change.
     *
     * The delegation below spells out `SeededPaymentRepository()` again
     * rather than writing `this(events = events)`: the primary constructor
     * is ALSO callable with just `events` named ([payments] defaulted),
     * which makes `this(events = events)` genuinely ambiguous between "the
     * primary, `payments` defaulted" and "this same constructor,
     * recursively" — Kotlin resolves that ambiguity as self-delegation, a
     * compile error ("cycle in the delegation calls chain"), reproduced and
     * confirmed while building this task. Supplying [payments] explicitly
     * makes the call unambiguously 2-argument, matching only the primary.
     * [PaymentRepository] wiring is still genuinely unchanged (same seeded
     * fixture, same default expression) — just written twice, because
     * Dagger/Hilt cannot call a constructor "with some arguments defaulted"
     * the way an ordinary Kotlin call site can.
     */
    @Inject
    public constructor(events: EventTracker) : this(payments = SeededPaymentRepository(), events = events)

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
        // Merge fix: ApiErrorReporter.report()'s signature changed (dropped
        // `screen` -- the app_event/v1 api_error variant has no screen field,
        // see ApiErrorReporter's own KDoc) after this call site was written;
        // method/path/status describe the simulated payment call this seeded
        // decline stands in for (POST /payments, 402 -- ApiError.kt's own
        // comment pins DAILY_LIMIT_EXCEEDED to 402).
        errorReporter.report(error = failure.code, method = "POST", path = "/payments", status = 402)
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
