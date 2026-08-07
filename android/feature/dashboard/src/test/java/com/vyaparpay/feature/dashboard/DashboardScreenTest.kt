package com.vyaparpay.feature.dashboard

import androidx.activity.ComponentActivity
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.assert
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import com.vyaparpay.core.analytics.AppEvent
import com.vyaparpay.core.analytics.EventTracker
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * Drives [DashboardScreen] through real Compose semantics (docs/03 §6's
 * Compose-UI-test layer), matching `PaymentScreenTest`'s Robolectric setup.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class DashboardScreenTest {

    @get:Rule
    val composeTestRule = createAndroidComposeRule<ComponentActivity>()

    private val loadedState = DashboardUiState(
        isLoading = false,
        walletBalancePaise = SeededDashboardRepository.WALLET_BALANCE_PAISE,
        collectionsTodayPaise = SeededDashboardRepository.COLLECTIONS_TODAY_PAISE,
        settlementEtaLabel = SeededDashboardRepository.SETTLEMENT_ETA_LABEL,
        alertMessage = SeededDashboardRepository.KYC_ALERT_MESSAGE,
    )

    private val ctaTags =
        listOf(PAY_VENDOR_TEST_TAG, VIEW_SETTLEMENTS_TEST_TAG, DEVICE_ORDERS_TEST_TAG, NEED_HELP_TEST_TAG)

    @Test
    fun `every documented testTag renders`() {
        composeTestRule.setContent {
            DashboardScreen(
                state = loadedState,
                events = RecordingEventTracker(),
                onPayVendor = {},
                onViewSettlements = {},
                onViewOrders = {},
                onNeedHelp = {},
            )
        }

        // The screen scrolls (more content than a small Robolectric viewport
        // fits), so each node is scrolled into view before asserting it is
        // displayed -- the standard Compose UI test pattern for scrollable
        // content; existence alone wouldn't catch a node rendered off-tree.
        composeTestRule.onNodeWithTag(DashboardDestination.ROOT_TEST_TAG).assertIsDisplayed()
        composeTestRule.onNodeWithTag("wallet_balance").performScrollTo().assertIsDisplayed()
        composeTestRule.onNodeWithTag("collections_today_balance").performScrollTo().assertIsDisplayed()
        composeTestRule.onNodeWithTag("settlement_eta_status").performScrollTo().assertIsDisplayed()
        composeTestRule.onNodeWithTag("kyc_alert").performScrollTo().assertIsDisplayed()
        composeTestRule.onNodeWithTag(PAY_VENDOR_TEST_TAG).performScrollTo().assertIsDisplayed()
        composeTestRule.onNodeWithTag(VIEW_SETTLEMENTS_TEST_TAG).performScrollTo().assertIsDisplayed()
        composeTestRule.onNodeWithTag(DEVICE_ORDERS_TEST_TAG).performScrollTo().assertIsDisplayed()
        composeTestRule.onNodeWithTag(NEED_HELP_TEST_TAG).performScrollTo().assertIsDisplayed()
    }

    @Test
    fun `quick action CTAs carry Role Button while enabled`() {
        composeTestRule.setContent {
            DashboardScreen(
                state = loadedState,
                events = RecordingEventTracker(),
                onPayVendor = {},
                onViewSettlements = {},
                onViewOrders = {},
                onNeedHelp = {},
            )
        }

        ctaTags.forEach { tag ->
            composeTestRule.onNodeWithTag(tag)
                .assert(SemanticsMatcher.expectValue(SemanticsProperties.Role, Role.Button))
        }
    }

    @Test
    fun `quick action CTAs carry Role Button while disabled during the initial load`() {
        // Review fix (T4.3, PaymentScreen's PayNowButton): the disabled
        // branch must carry Role.Button too, since the node is disabled
        // during loading, not absent from the tree.
        composeTestRule.setContent {
            DashboardScreen(
                state = DashboardUiState(isLoading = true),
                events = RecordingEventTracker(),
                onPayVendor = {},
                onViewSettlements = {},
                onViewOrders = {},
                onNeedHelp = {},
            )
        }

        ctaTags.forEach { tag ->
            composeTestRule.onNodeWithTag(tag)
                .assert(SemanticsMatcher.expectValue(SemanticsProperties.Role, Role.Button))
        }
    }

    @Test
    fun `tapping a quick action records a Tap event named for its testTag`() {
        val tracker = RecordingEventTracker()
        var tapped = false
        composeTestRule.setContent {
            DashboardScreen(
                state = loadedState,
                events = tracker,
                onPayVendor = { tapped = true },
                onViewSettlements = {},
                onViewOrders = {},
                onNeedHelp = {},
            )
        }

        composeTestRule.onNodeWithTag(PAY_VENDOR_TEST_TAG).performClick()

        assertTrue(tapped)
        assertTrue(
            "expected a tap event named $PAY_VENDOR_TEST_TAG",
            tracker.events.any { it is AppEvent.Tap && it.name == PAY_VENDOR_TEST_TAG },
        )
    }

    private class RecordingEventTracker : EventTracker {
        val events = mutableListOf<AppEvent>()
        override fun record(event: AppEvent) {
            events.add(event)
        }
        override fun recent(count: Int): List<AppEvent> = events.takeLast(count).asReversed()
        override val lastAction: AppEvent? get() = events.lastOrNull()
    }
}
