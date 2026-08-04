package com.vyaparpay.feature.payments

import androidx.activity.ComponentActivity
import androidx.compose.runtime.getValue
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.semantics.getOrNull
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.assert
import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * Drives [SettlementsScreen] through real Compose semantics (docs/03 §6's
 * Compose-UI-test layer). Robolectric, matching [PaymentScreenTest]'s own
 * reasoning: `testDebugUnitTest` is the only gate this gets to be part of.
 *
 * Deliberately does NOT attempt a full UiTreeCollector -> SemanticSnapshotBuilder
 * -> DropLadder integration test against this screen (a
 * `SettlementsScreenContextCanaryTest`) -- `:core:screencontext`'s
 * `feat/semantic-snapshot-builder` branch is still under independent review
 * and not on `main` as of this task's start. These tests check only the raw
 * Compose semantics this screen itself is responsible for producing.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34], qualifiers = "w411dp-h891dp") // Robolectric's own default window is far shorter than a real phone -- too short for this screen's header + weighted list to both fit; a normal phone size (Pixel-5-ish) is what production actually renders on.
class SettlementsScreenTest {

    @get:Rule
    val composeTestRule = createAndroidComposeRule<ComponentActivity>()

    private fun sampleState(shortfallMessage: String? = "3 batches flagged for shortfall"): SettlementsUiState =
        SettlementsUiState(
            isLoading = false,
            netSettledValue = "₹12,34,567.00",
            feesValue = "₹14,814.00",
            latestBatchStatus = "SETTLED",
            shortfallMessage = shortfallMessage,
            rows = (0 until 34).map { index ->
                SettlementRowUiModel(
                    settlementId = "setl_test_$index",
                    label = "$index Jul 2026",
                    value = "₹1,000.00 · SETTLED",
                )
            },
            totalCount = 34,
        )

    @Test
    fun `root and every header node render with the docs 03 section 4 testTags`() {
        composeTestRule.setContent {
            SettlementsScreen(state = sampleState(), events = InMemoryEventTracker(), onExportCsvClicked = {})
        }

        composeTestRule.onNodeWithTag(SettlementsDestination.ROOT_TEST_TAG).assertIsDisplayed()
        composeTestRule.onNodeWithTag("settlements_net_balance").assertIsDisplayed()
        composeTestRule.onNodeWithTag("settlements_fees_balance").assertIsDisplayed()
        composeTestRule.onNodeWithTag("latest_batch_status").assertIsDisplayed()
        composeTestRule.onNodeWithTag("shortfall_alert").assertIsDisplayed()
        composeTestRule.onNodeWithTag(EXPORT_CSV_TEST_TAG).assertIsDisplayed()
        composeTestRule.onNodeWithTag(SETTLEMENTS_LIST_TEST_TAG).assertIsDisplayed()
    }

    @Test
    fun `the shortfall alert is absent when the state reports no shortfall`() {
        composeTestRule.setContent {
            SettlementsScreen(
                state = sampleState(shortfallMessage = null),
                events = InMemoryEventTracker(),
                onExportCsvClicked = {},
            )
        }

        composeTestRule.onNodeWithTag("shortfall_alert").assertDoesNotExist()
    }

    @Test
    fun `the batches list reports 34 as its collection total, independent of how many rows are realized on screen`() {
        composeTestRule.setContent {
            SettlementsScreen(state = sampleState(), events = InMemoryEventTracker(), onExportCsvClicked = {})
        }

        composeTestRule.onNodeWithTag(SETTLEMENTS_LIST_TEST_TAG).assert(
            SemanticsMatcher("CollectionInfo.rowCount == 34") { node ->
                node.config.getOrNull(SemanticsProperties.CollectionInfo)?.rowCount == 34
            },
        )
    }

    @Test
    fun `a realized row carries CollectionItemInfo, the signal UiTreeCollector uses to recognize a list item`() {
        composeTestRule.setContent {
            SettlementsScreen(state = sampleState(), events = InMemoryEventTracker(), onExportCsvClicked = {})
        }

        composeTestRule.onNodeWithTag("settlement_batch_row_0")
            .assert(SemanticsMatcher.keyIsDefined(SemanticsProperties.CollectionItemInfo))
    }

    @Test
    fun `no row carries a _status-suffixed testTag -- the exact hazard that silently breaks visible_count`() {
        // TestTagRoles.roleFor resolves any `_status` suffix to STATUS_BADGE
        // (docs/03 §4). A per-row status badge would insert a second
        // role-resolvable node between consecutive `list_item`s, breaking the
        // `[list, list_item, list_item, ...]` contiguity
        // SemanticSnapshotBuilder.fillListVisibleCounts and
        // DropLadder.rung1CapListItems both depend on (this task's own
        // brief). The only testTag anywhere in this screen allowed to end in
        // `_status` is the header's `latest_batch_status`, which sits OUTSIDE
        // the list -- so exactly one node in the whole tree should carry a
        // `_status`-suffixed tag.
        composeTestRule.setContent {
            SettlementsScreen(state = sampleState(), events = InMemoryEventTracker(), onExportCsvClicked = {})
        }

        composeTestRule.onAllNodes(
            SemanticsMatcher("testTag ends with _status") { node ->
                node.config.getOrNull(SemanticsProperties.TestTag)?.endsWith("_status") == true
            },
            useUnmergedTree = true,
        ).assertCountEquals(1)
    }

    @Test
    fun `wired to the real SettlementsViewModel, the seeded repository renders all 34 rows`() {
        val viewModel = SettlementsViewModel(settlements = SeededSettlementsRepository())

        composeTestRule.setContent {
            val state by viewModel.state.collectAsStateWithLifecycle()
            SettlementsScreen(state = state, events = viewModel.events, onExportCsvClicked = {})
        }
        composeTestRule.waitForIdle()

        composeTestRule.onNodeWithTag(SETTLEMENTS_LIST_TEST_TAG).assert(
            SemanticsMatcher("CollectionInfo.rowCount == 34") { node ->
                node.config.getOrNull(SemanticsProperties.CollectionInfo)?.rowCount == 34
            },
        )
    }
}
