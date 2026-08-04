package com.vyaparpay.feature.payments

import androidx.activity.ComponentActivity
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.semantics.getOrNull
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.assert
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/** Drives [OrdersScreen] through real Compose semantics, Robolectric-hosted (same reasoning as [PaymentScreenTest]). */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class OrdersScreenTest {

    @get:Rule
    val composeTestRule = createAndroidComposeRule<ComponentActivity>()

    private fun sampleState(): OrdersUiState = OrdersUiState(
        isLoading = false,
        rows = listOf(
            OrderRowUiModel(orderId = "ord_snd_0712", label = "VyaparPay Soundbox", value = "IN_TRANSIT · Delhivery"),
            OrderRowUiModel(orderId = "ord_qr_0705", label = "VyaparPay QR Kit", value = "DELIVERED · Delhivery"),
            OrderRowUiModel(orderId = "ord_snd_0725", label = "VyaparPay Soundbox", value = "PLACED"),
        ),
        totalCount = 3,
    )

    @Test
    fun `root, the orders list and Track Order CTA render with the docs 03 section 4 testTags`() {
        composeTestRule.setContent {
            OrdersScreen(
                state = sampleState(),
                events = InMemoryEventTracker(),
                onOrderRowClicked = {},
                onTrackOrderClicked = {},
            )
        }

        composeTestRule.onNodeWithTag(OrdersDestination.ROOT_TEST_TAG).assertIsDisplayed()
        composeTestRule.onNodeWithTag(ORDERS_LIST_TEST_TAG).assertIsDisplayed()
        composeTestRule.onNodeWithTag(TRACK_ORDER_TEST_TAG).assertIsDisplayed()
    }

    @Test
    fun `Track Order carries explicit Role Button semantics`() {
        composeTestRule.setContent {
            OrdersScreen(
                state = sampleState(),
                events = InMemoryEventTracker(),
                onOrderRowClicked = {},
                onTrackOrderClicked = {},
            )
        }

        composeTestRule.onNodeWithTag(TRACK_ORDER_TEST_TAG)
            .assert(SemanticsMatcher.expectValue(SemanticsProperties.Role, Role.Button))
    }

    @Test
    fun `tapping a device order row reports that row's own order id`() {
        var selected: String? = null
        composeTestRule.setContent {
            OrdersScreen(
                state = sampleState(),
                events = InMemoryEventTracker(),
                onOrderRowClicked = { selected = it },
                onTrackOrderClicked = {},
            )
        }

        composeTestRule.onNodeWithTag("device_order_row_1").performClick()

        assertEquals("ord_qr_0705", selected)
    }

    @Test
    fun `tapping Track Order invokes its own callback, independent of row taps`() {
        var trackTapped = false
        composeTestRule.setContent {
            OrdersScreen(
                state = sampleState(),
                events = InMemoryEventTracker(),
                onOrderRowClicked = {},
                onTrackOrderClicked = { trackTapped = true },
            )
        }

        composeTestRule.onNodeWithTag(TRACK_ORDER_TEST_TAG).performClick()

        assertEquals(true, trackTapped)
    }

    @Test
    fun `a realized row carries CollectionItemInfo, the signal UiTreeCollector uses to recognize a list item`() {
        composeTestRule.setContent {
            OrdersScreen(
                state = sampleState(),
                events = InMemoryEventTracker(),
                onOrderRowClicked = {},
                onTrackOrderClicked = {},
            )
        }

        composeTestRule.onNodeWithTag("device_order_row_0")
            .assert(SemanticsMatcher.keyIsDefined(SemanticsProperties.CollectionItemInfo))
    }

    @Test
    fun `no row carries a _status-suffixed testTag`() {
        composeTestRule.setContent {
            OrdersScreen(
                state = sampleState(),
                events = InMemoryEventTracker(),
                onOrderRowClicked = {},
                onTrackOrderClicked = {},
            )
        }

        composeTestRule.onNodeWithTag("device_order_row_0")
            .assert(
                SemanticsMatcher("no _status-suffixed testTag") { node ->
                    node.config.getOrNull(SemanticsProperties.TestTag)?.endsWith("_status") != true
                },
            )
    }
}
