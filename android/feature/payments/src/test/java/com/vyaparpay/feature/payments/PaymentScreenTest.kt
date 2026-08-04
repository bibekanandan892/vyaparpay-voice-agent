package com.vyaparpay.feature.payments

import androidx.activity.ComponentActivity
import androidx.compose.runtime.getValue
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsNotDisplayed
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextInput
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.vyaparpay.core.network.ApiError
import com.vyaparpay.core.network.ApiResult
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * Drives [PaymentScreen] through real Compose semantics (docs/03
 * §6's Compose-UI-test layer) — the amount-entry -> Pay Now -> decline-dialog
 * flow that reproduces the canonical Rajesh Kumar / ₹245 / `Amazon Business` /
 * `DAILY_LIMIT_EXCEEDED` incident (docs/01 §7 steps 1-2).
 *
 * Runs under Robolectric rather than as an instrumented `androidTest`: there
 * is no connectedAndroidTest job in android.yml and no device in this
 * environment, so `testDebugUnitTest` — which android.yml does run — is the
 * only gate this gets to be part of.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class PaymentScreenTest {

    @get:Rule
    val composeTestRule = createAndroidComposeRule<ComponentActivity>()

    @Test
    fun `amount field, recipient row and Pay Now CTA render with the canonical roles' testTags`() {
        composeTestRule.setContent {
            PaymentScreen(
                state = PaymentUiState(),
                events = InMemoryEventTracker(),
                onAmountChanged = {},
                onPayNowClicked = {},
                onDismissDialog = {},
                onSnackbarShown = {},
            )
        }

        composeTestRule.onNodeWithTag(PaymentDestination.ROOT_TEST_TAG).assertIsDisplayed()
        composeTestRule.onNodeWithTag("amount_input").assertIsDisplayed()
        composeTestRule.onNodeWithTag("recipient_row").assertIsDisplayed()
        composeTestRule.onNodeWithTag(PAY_NOW_TEST_TAG).assertIsDisplayed()
    }

    @Test
    fun `typing 245 and tapping Pay Now surfaces the daily limit decline dialog and snackbar`() {
        val viewModel = PaymentViewModel(payments = SeededPaymentRepository())

        composeTestRule.setContent {
            val state by viewModel.state.collectAsStateWithLifecycle()
            PaymentScreen(
                state = state,
                events = viewModel.events,
                onAmountChanged = viewModel::onAmountChanged,
                onPayNowClicked = viewModel::onPayNowClicked,
                onDismissDialog = viewModel::onDeclineDialogDismissed,
                onSnackbarShown = viewModel::onSnackbarShown,
            )
        }

        composeTestRule.onNodeWithTag("amount_input").performTextInput("245")
        composeTestRule.onNodeWithTag(PAY_NOW_TEST_TAG).performClick()
        composeTestRule.waitForIdle()

        composeTestRule.onNodeWithText("Daily Limit Exceeded").assertIsDisplayed()
        composeTestRule.onNodeWithTag("payment_snackbar").assertIsDisplayed()
    }

    @Test
    fun `dismissing the decline dialog removes it from the tree`() {
        val viewModel = PaymentViewModel(
            payments = object : PaymentRepository {
                override suspend fun payVendor(amountPaise: Long, recipient: String) =
                    ApiResult.Failure(
                        code = ApiError.DAILY_LIMIT_EXCEEDED,
                        message = "Your daily transaction limit has been reached.",
                    )
            },
        )

        composeTestRule.setContent {
            val state by viewModel.state.collectAsStateWithLifecycle()
            PaymentScreen(
                state = state,
                events = viewModel.events,
                onAmountChanged = viewModel::onAmountChanged,
                onPayNowClicked = viewModel::onPayNowClicked,
                onDismissDialog = viewModel::onDeclineDialogDismissed,
                onSnackbarShown = viewModel::onSnackbarShown,
            )
        }

        composeTestRule.onNodeWithTag("amount_input").performTextInput("245")
        composeTestRule.onNodeWithTag(PAY_NOW_TEST_TAG).performClick()
        composeTestRule.waitForIdle()
        composeTestRule.onNodeWithText("Daily Limit Exceeded").assertIsDisplayed()

        composeTestRule.onNodeWithTag("daily_limit_dismiss_secondary").performClick()
        composeTestRule.waitForIdle()

        composeTestRule.onNodeWithText("Daily Limit Exceeded").assertIsNotDisplayed()
    }
}
