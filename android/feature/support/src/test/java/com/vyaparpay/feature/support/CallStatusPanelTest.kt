package com.vyaparpay.feature.support

import androidx.activity.ComponentActivity
import androidx.compose.runtime.Composable
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import com.vyaparpay.core.analytics.AppEvent
import com.vyaparpay.core.analytics.EventTracker
import com.vyaparpay.voice.EndReason
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * [CallStatusPanel] through real Compose semantics, matching [HelpScreenTest]'s
 * Robolectric setup.
 *
 * Scope note: this covers the trigger path's UI only — a call's state, and the
 * controls that end or dismiss it. There is deliberately no transcript
 * assertion here, because there is deliberately no transcript rendering; see
 * [CallStatusPanel]'s kdoc for why that seam stays closed in this task.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class CallStatusPanelTest {

    @get:Rule
    val composeTestRule = createAndroidComposeRule<ComponentActivity>()

    @Test
    fun `an idle state with no notice renders nothing at all`() {
        composeTestRule.setContent { Panel(CallUiState()) }

        composeTestRule.onNodeWithTag(CALL_PANEL_TEST_TAG).assertDoesNotExist()
    }

    @Test
    fun `a connecting call shows its status and offers a hang up`() {
        composeTestRule.setContent { Panel(CallUiState(phase = CallPhase.CONNECTING)) }

        composeTestRule.onNodeWithTag(CALL_PANEL_TEST_TAG).assertIsDisplayed()
        composeTestRule.onNodeWithTag(CALL_STATUS_TEST_TAG).assertIsDisplayed()
        composeTestRule.onNodeWithTag(CALL_HANG_UP_TEST_TAG).assertIsDisplayed()
    }

    @Test
    fun `an in-call state offers a hang up and no dismiss`() {
        // Dismissing a live call would hide the only control that ends it —
        // and with it the merchant's awareness that the mic is open.
        composeTestRule.setContent { Panel(CallUiState(phase = CallPhase.IN_CALL)) }

        composeTestRule.onNodeWithTag(CALL_HANG_UP_TEST_TAG).assertIsDisplayed()
        composeTestRule.onNodeWithTag(CALL_DISMISS_TEST_TAG).assertDoesNotExist()
    }

    @Test
    fun `tapping End Call invokes the hang up callback`() {
        var hungUp = false
        composeTestRule.setContent {
            Panel(CallUiState(phase = CallPhase.IN_CALL), onHangUp = { hungUp = true })
        }

        composeTestRule.onNodeWithTag(CALL_HANG_UP_TEST_TAG).performClick()

        assertTrue(hungUp)
    }

    @Test
    fun `an ended call offers dismiss instead of hang up`() {
        composeTestRule.setContent {
            Panel(CallUiState(phase = CallPhase.ENDED, endReason = EndReason.USER_HUNG_UP))
        }

        composeTestRule.onNodeWithTag(CALL_HANG_UP_TEST_TAG).assertDoesNotExist()
        composeTestRule.onNodeWithTag(CALL_DISMISS_TEST_TAG).assertIsDisplayed()
    }

    @Test
    fun `a permanently blocked microphone offers the settings route out`() {
        // The whole point of the PERMANENT_DENIAL branch: not a silent no-op.
        var openedSettings = false
        composeTestRule.setContent {
            Panel(
                CallUiState(notice = CallNotice.MIC_BLOCKED),
                onOpenSettings = { openedSettings = true },
            )
        }

        composeTestRule.onNodeWithTag(CALL_SETTINGS_TEST_TAG).performClick()

        assertTrue(openedSettings)
    }

    @Test
    fun `a retryable denial does not offer settings, only a dismiss`() {
        // The way back in is tapping Call Support again, not a trip through
        // system settings for a permission the OS will still prompt for.
        composeTestRule.setContent { Panel(CallUiState(notice = CallNotice.MIC_DENIED)) }

        composeTestRule.onNodeWithTag(CALL_SETTINGS_TEST_TAG).assertDoesNotExist()
        composeTestRule.onNodeWithTag(CALL_DISMISS_TEST_TAG).assertIsDisplayed()
    }

    @Test
    fun `the hang up control records a tap on the shared event timeline`() {
        // SupportButton routes through trackedClickable, so ending a call
        // lands on the same timeline the agent reads as the tap that started
        // it (SupportRoute passes HelpViewModel's tracker to both).
        val events = RecordingEventTracker()
        composeTestRule.setContent { Panel(CallUiState(phase = CallPhase.IN_CALL), events = events) }

        composeTestRule.onNodeWithTag(CALL_HANG_UP_TEST_TAG).performClick()

        assertEquals(1, events.events.size)
        assertTrue(events.events.single() is AppEvent.Tap)
    }

    @Composable
    private fun Panel(
        state: CallUiState,
        events: EventTracker = RecordingEventTracker(),
        onHangUp: () -> Unit = {},
        onDismiss: () -> Unit = {},
        onOpenSettings: () -> Unit = {},
    ) {
        CallStatusPanel(
            state = state,
            events = events,
            onHangUp = onHangUp,
            onDismiss = onDismiss,
            onOpenSettings = onOpenSettings,
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
