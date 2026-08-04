package com.vyaparpay.feature.payments

import androidx.activity.ComponentActivity
import androidx.compose.runtime.getValue
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.assert
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * Drives [OrderTrackingScreen] through real Compose semantics, Robolectric-hosted
 * (same reasoning as [PaymentScreenTest]). There is no separate
 * `OrderTrackingViewModelTest` in this task's file list -- the last test here
 * wires the real [OrderTrackingViewModel] to cover it.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class OrderTrackingScreenTest {

    @get:Rule
    val composeTestRule = createAndroidComposeRule<ComponentActivity>()

    private val sampleState = OrderTrackingUiState(isLoading = false, statusValue = "In transit", etaValue = "26 Jul 2026")

    @Test
    fun `root and both status rows render with the docs 03 section 4 testTags`() {
        composeTestRule.setContent {
            OrderTrackingScreen(state = sampleState, events = InMemoryEventTracker(), onBackClicked = {})
        }

        composeTestRule.onNodeWithTag(OrderTrackingDestination.ROOT_TEST_TAG).assertIsDisplayed()
        composeTestRule.onNodeWithTag(TRACKER_STATE_TEST_TAG).assertIsDisplayed()
        composeTestRule.onNodeWithTag(ORDER_ETA_TEST_TAG).assertIsDisplayed()
        composeTestRule.onNodeWithTag(BACK_TO_ORDERS_TEST_TAG).assertIsDisplayed()
    }

    @Test
    fun `Back to orders carries explicit Role Button semantics`() {
        composeTestRule.setContent {
            OrderTrackingScreen(state = sampleState, events = InMemoryEventTracker(), onBackClicked = {})
        }

        composeTestRule.onNodeWithTag(BACK_TO_ORDERS_TEST_TAG)
            .assert(SemanticsMatcher.expectValue(SemanticsProperties.Role, Role.Button))
    }

    @Test
    fun `tapping Back to orders invokes the callback`() {
        var backTapped = false
        composeTestRule.setContent {
            OrderTrackingScreen(state = sampleState, events = InMemoryEventTracker(), onBackClicked = { backTapped = true })
        }

        composeTestRule.onNodeWithTag(BACK_TO_ORDERS_TEST_TAG).performClick()

        assertTrue(backTapped)
    }

    @Test
    fun `wired to the real OrderTrackingViewModel, the canonical in-transit soundbox renders`() {
        val viewModel = OrderTrackingViewModel(orders = SeededOrdersRepository())

        composeTestRule.setContent {
            val state by viewModel.state.collectAsStateWithLifecycle()
            OrderTrackingScreen(state = state, events = viewModel.events, onBackClicked = {})
        }
        composeTestRule.waitForIdle()

        composeTestRule.onNodeWithText("In transit").assertIsDisplayed()
        composeTestRule.onNodeWithText("26 Jul 2026").assertIsDisplayed()
    }
}
