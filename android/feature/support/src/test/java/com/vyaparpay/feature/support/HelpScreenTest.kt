package com.vyaparpay.feature.support

import androidx.activity.ComponentActivity
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
 * Drives [HelpScreen] through real Compose semantics (docs/03 §6's
 * Compose-UI-test layer), matching `PaymentScreenTest`'s Robolectric setup.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class HelpScreenTest {

    @get:Rule
    val composeTestRule = createAndroidComposeRule<ComponentActivity>()

    private val loadedState = HelpUiState(
        isLoading = false,
        faqs = SeededSupportRepository.SEEDED_FAQS,
        complaints = SeededSupportRepository.SEEDED_COMPLAINTS,
    )

    @Test
    fun `FAQ and complaint rows render with their documented testTags`() {
        composeTestRule.setContent {
            HelpScreen(state = loadedState, events = RecordingEventTracker(), onCallSupport = {})
        }

        // The screen scrolls (more content than a small Robolectric viewport
        // fits), so each node is scrolled into view before asserting it is
        // displayed -- the standard Compose UI test pattern for scrollable
        // content; existence alone wouldn't catch a node rendered off-tree.
        composeTestRule.onNodeWithTag(SupportDestination.ROOT_TEST_TAG).assertIsDisplayed()
        composeTestRule.onNodeWithTag(CALL_SUPPORT_TEST_TAG).performScrollTo().assertIsDisplayed()
        composeTestRule.onNodeWithTag(TEXT_CHAT_TEST_TAG).performScrollTo().assertIsDisplayed()
        composeTestRule.onNodeWithTag("faq_list").performScrollTo().assertIsDisplayed()
        composeTestRule.onNodeWithTag("faq_item_0").performScrollTo().assertIsDisplayed()
        composeTestRule.onNodeWithTag("faq_item_${SeededSupportRepository.SEEDED_FAQS.lastIndex}").performScrollTo().assertIsDisplayed()
        composeTestRule.onNodeWithTag("complaint_row_0").performScrollTo().assertIsDisplayed()
    }

    @Test
    fun `tapping Call Support invokes the callback`() {
        var called = false
        composeTestRule.setContent {
            HelpScreen(state = loadedState, events = RecordingEventTracker(), onCallSupport = { called = true })
        }

        composeTestRule.onNodeWithTag(CALL_SUPPORT_TEST_TAG).performClick()

        assertTrue(called)
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
