package com.vyaparpay.feature.payments

import app.cash.turbine.test
import com.vyaparpay.core.analytics.AppEvent
import com.vyaparpay.core.analytics.EventTracker
import com.vyaparpay.core.network.ApiError
import com.vyaparpay.core.network.ApiResult
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * [PaymentViewModel] state-transition coverage (docs/03-android-architecture.md
 * §6: "ViewModel unit tests ... state transitions ... envelope failures mapped
 * to `ApiResult.Failure`"), independent of Compose — the happy path and the
 * canonical Rajesh Kumar / ₹245 / `DAILY_LIMIT_EXCEEDED` decline path
 * (docs/01-product-and-use-case.md §7 steps 1-2).
 */
@OptIn(ExperimentalCoroutinesApi::class)
class PaymentViewModelTest {

    private val dispatcher = StandardTestDispatcher()

    @Before
    fun setUp() {
        Dispatchers.setMain(dispatcher)
    }

    @After
    fun tearDown() {
        Dispatchers.resetMain()
    }

    @Test
    fun `initial state carries the canonical recipient and an empty amount`() {
        val viewModel = PaymentViewModel(payments = ApprovingRepository())

        val state = viewModel.state.value

        assertEquals(SeededPaymentRepository.CANONICAL_RECIPIENT, state.recipient)
        assertTrue(state.amountInput.isEmpty())
        assertNull(state.declineDialog)
        assertNull(state.snackbar)
    }

    @Test
    fun `onAmountChanged keeps only digits`() {
        val viewModel = PaymentViewModel(payments = ApprovingRepository())

        viewModel.onAmountChanged("2a4b5")

        assertEquals("245", viewModel.state.value.amountInput)
    }

    @Test
    fun `Pay Now with an empty amount is rejected before the repository is called`() = runTest {
        val repository = CountingRepository()
        val viewModel = PaymentViewModel(payments = repository)

        viewModel.onPayNowClicked()

        assertEquals(0, repository.callCount)
        assertNotNull(viewModel.state.value.amountError)
    }

    @Test
    fun `an approved payment clears the amount and shows a success snackbar`() = runTest {
        val viewModel = PaymentViewModel(payments = ApprovingRepository())
        viewModel.onAmountChanged("100")

        viewModel.state.test {
            assertEquals("100", awaitItem().amountInput)

            viewModel.onPayNowClicked()
            assertTrue(awaitItem().isSubmitting)

            dispatcher.scheduler.advanceUntilIdle()
            val approved = awaitItem()
            assertTrue(approved.amountInput.isEmpty())
            assertNull(approved.declineDialog)
            assertNotNull(approved.snackbar)

            cancelAndIgnoreRemainingEvents()
        }
    }

    @Test
    fun `the canonical 245 rupee payment is declined as DAILY_LIMIT_EXCEEDED`() = runTest {
        val tracker = RecordingEventTracker()
        val viewModel = PaymentViewModel(payments = SeededPaymentRepository(), events = tracker)
        viewModel.onAmountChanged((SeededPaymentRepository.CANONICAL_AMOUNT_PAISE / 100).toString())

        viewModel.onPayNowClicked()
        dispatcher.scheduler.advanceUntilIdle()

        val state = viewModel.state.value
        assertEquals(ApiError.DAILY_LIMIT_EXCEEDED, state.declineDialog?.code)
        assertEquals("Daily Limit Exceeded", state.declineDialog?.title)
        assertNotNull(state.snackbar)
        assertTrue(
            "expected an api_error timeline entry for DAILY_LIMIT_EXCEEDED",
            // Merge fix: AppEvent became a sealed interface (Phase-4 T7) --
            // `target` was renamed `name` and moved behind the ApiErrorEvent
            // variant; the wire-shape `code` field is what actually carries
            // the ApiError, so that's what this assertion checks.
            tracker.events.any {
                it is AppEvent.ApiErrorEvent && it.code == ApiError.DAILY_LIMIT_EXCEEDED.name
            },
        )
    }

    @Test
    fun `a payment that stays under the daily limit is approved`() = runTest {
        val viewModel = PaymentViewModel(payments = SeededPaymentRepository())
        // Only ₹110 of headroom remains (docs/01 §8 turn 3); ₹50 clears it.
        viewModel.onAmountChanged("50")

        viewModel.onPayNowClicked()
        dispatcher.scheduler.advanceUntilIdle()

        assertNull(viewModel.state.value.declineDialog)
    }

    @Test
    fun `dismissing the decline dialog clears it without touching the snackbar`() = runTest {
        val viewModel = PaymentViewModel(payments = SeededPaymentRepository())
        viewModel.onAmountChanged((SeededPaymentRepository.CANONICAL_AMOUNT_PAISE / 100).toString())
        viewModel.onPayNowClicked()
        dispatcher.scheduler.advanceUntilIdle()
        assertNotNull(viewModel.state.value.declineDialog)

        viewModel.onDeclineDialogDismissed()

        val state = viewModel.state.value
        assertNull(state.declineDialog)
        assertNotNull(state.snackbar)
    }

    @Test
    fun `onSnackbarShown clears the snackbar`() = runTest {
        val viewModel = PaymentViewModel(payments = ApprovingRepository())
        viewModel.onAmountChanged("100")
        viewModel.onPayNowClicked()
        dispatcher.scheduler.advanceUntilIdle()
        assertNotNull(viewModel.state.value.snackbar)

        viewModel.onSnackbarShown()

        assertNull(viewModel.state.value.snackbar)
    }

    private class RecordingEventTracker : EventTracker {
        val events = mutableListOf<AppEvent>()
        override fun record(event: AppEvent) {
            events.add(event)
        }
        override fun recent(count: Int): List<AppEvent> = events.takeLast(count).asReversed()
        override val lastAction: AppEvent? get() = events.lastOrNull()
    }

    private class ApprovingRepository : PaymentRepository {
        override suspend fun payVendor(amountPaise: Long, recipient: String) =
            ApiResult.Success(PaymentReceipt(paymentId = "pay_test", recipient = recipient, amountPaise = amountPaise))
    }

    private class CountingRepository : PaymentRepository {
        var callCount: Int = 0
            private set

        override suspend fun payVendor(amountPaise: Long, recipient: String): ApiResult<PaymentReceipt> {
            callCount++
            return ApiResult.Success(PaymentReceipt(paymentId = "pay_test", recipient = recipient, amountPaise = amountPaise))
        }
    }
}
