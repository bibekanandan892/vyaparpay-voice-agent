package com.vyaparpay.feature.support

import androidx.activity.ComponentActivity
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.click
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performTouchInput
import com.vyaparpay.core.analytics.AppEvent
import com.vyaparpay.core.analytics.EventTracker
import com.vyaparpay.voice.EndReason
import org.junit.Assert.assertFalse
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * [CallSurface] proves the one guarantee neither [CallScreenTest] nor
 * [CallStatusPanelTest] can on their own: that [SupportRoute] never mounts
 * both surfaces at once. Each phase drives a real `Box { CallSurface(...) }`
 * and asserts exactly one of [CALL_SCREEN_TEST_TAG] / [CALL_PANEL_TEST_TAG]
 * is present — the mutual exclusivity a code review can read off
 * `CallUiState.isFullScreenCall`'s exhaustive `when`, made a regression test
 * instead of something only true until someone edits that `when`.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class CallSurfaceTest {

    @get:Rule
    val composeTestRule = createAndroidComposeRule<ComponentActivity>()

    @Test
    fun `connecting shows the full call screen, never the banner`() {
        setSurface(CallUiState(phase = CallPhase.CONNECTING))

        composeTestRule.onNodeWithTag(CALL_SCREEN_TEST_TAG).assertIsDisplayed()
        composeTestRule.onNodeWithTag(CALL_PANEL_TEST_TAG).assertDoesNotExist()
    }

    @Test
    fun `in call shows the full call screen, never the banner`() {
        setSurface(CallUiState(phase = CallPhase.IN_CALL))

        composeTestRule.onNodeWithTag(CALL_SCREEN_TEST_TAG).assertIsDisplayed()
        composeTestRule.onNodeWithTag(CALL_PANEL_TEST_TAG).assertDoesNotExist()
    }

    @Test
    fun `reconnecting shows the full call screen, never the banner`() {
        setSurface(CallUiState(phase = CallPhase.RECONNECTING))

        composeTestRule.onNodeWithTag(CALL_SCREEN_TEST_TAG).assertIsDisplayed()
        composeTestRule.onNodeWithTag(CALL_PANEL_TEST_TAG).assertDoesNotExist()
    }

    @Test
    fun `an ended call shows the banner, never the full call screen`() {
        setSurface(CallUiState(phase = CallPhase.ENDED, endReason = EndReason.USER_HUNG_UP))

        composeTestRule.onNodeWithTag(CALL_PANEL_TEST_TAG).assertIsDisplayed()
        composeTestRule.onNodeWithTag(CALL_SCREEN_TEST_TAG).assertDoesNotExist()
    }

    @Test
    fun `a permission notice shows the banner, never the full call screen`() {
        setSurface(CallUiState(notice = CallNotice.MIC_BLOCKED))

        composeTestRule.onNodeWithTag(CALL_PANEL_TEST_TAG).assertIsDisplayed()
        composeTestRule.onNodeWithTag(CALL_SCREEN_TEST_TAG).assertDoesNotExist()
    }

    @Test
    fun `idle with no notice shows neither surface`() {
        setSurface(CallUiState())

        composeTestRule.onNodeWithTag(CALL_SCREEN_TEST_TAG).assertDoesNotExist()
        composeTestRule.onNodeWithTag(CALL_PANEL_TEST_TAG).assertDoesNotExist()
    }

    @Test
    fun `leaving the full call screen does not itself end the call`() {
        // CallViewModel.onCleared's own kdoc: navigating away must not hang
        // up on the agent mid-sentence — the service keeps the call running,
        // only the observation stops. CallSurface's job is picking which
        // composable renders; it must not invent a hang-up trigger of its
        // own when the phase transitions out from under it.
        var hungUp = false
        val state = mutableStateOf(CallUiState(phase = CallPhase.IN_CALL))
        composeTestRule.setContent {
            Box(modifier = Modifier.fillMaxSize()) {
                CallSurface(
                    state = state.value,
                    events = RecordingEventTracker(),
                    onHangUp = { hungUp = true },
                    onDismiss = {},
                    onToggleMute = {},
                    onToggleSpeaker = {},
                    onOpenSettings = {},
                )
            }
        }

        state.value = CallUiState(phase = CallPhase.ENDED, endReason = EndReason.USER_HUNG_UP)
        composeTestRule.waitForIdle()

        assertFalse(hungUp)
    }

    @Test
    fun `taps on blank call-screen area do not reach the help screen underneath`() {
        // SupportRoute composes HelpScreen and then CallSurface in ONE Box,
        // so HelpScreen -- with its full-width "Call Support" button -- stays
        // composed and hit-testable beneath the full call screen for the
        // whole call. Compose hit-testing keeps descending to earlier
        // siblings until something with a pointer-input node claims the
        // touch; a plain background is not one. This stands in for
        // HelpScreen with a fill-size clickable and taps a blank corner of
        // the call screen (top-left, inside its own padding, nowhere near a
        // control): the tap must be swallowed by CallScreen, never delivered
        // underneath. Verified to FAIL when CallScreen's root is a bare
        // Column with a background instead of a Surface.
        var underneathTapped = false
        composeTestRule.setContent {
            Box(modifier = Modifier.fillMaxSize()) {
                Box(
                    modifier = Modifier
                        .fillMaxSize()
                        .testTag("underneath")
                        .clickable { underneathTapped = true },
                )
                CallSurface(
                    state = CallUiState(phase = CallPhase.IN_CALL, inCallSinceMillis = 0L),
                    events = RecordingEventTracker(),
                    onHangUp = {},
                    onDismiss = {},
                    onToggleMute = {},
                    onToggleSpeaker = {},
                    onOpenSettings = {},
                )
            }
        }

        composeTestRule.onNodeWithTag(CALL_SCREEN_TEST_TAG)
            .performTouchInput { click(Offset(2f, 2f)) }
        composeTestRule.waitForIdle()

        assertFalse("a blank-area tap on the call screen leaked to the screen beneath", underneathTapped)
    }

    private fun setSurface(state: CallUiState) {
        composeTestRule.setContent {
            Box(modifier = Modifier.fillMaxSize()) {
                CallSurface(
                    state = state,
                    events = RecordingEventTracker(),
                    onHangUp = {},
                    onDismiss = {},
                    onToggleMute = {},
                    onToggleSpeaker = {},
                    onOpenSettings = {},
                )
            }
        }
    }

    private class RecordingEventTracker : EventTracker {
        override fun record(event: AppEvent) = Unit
        override fun recent(count: Int): List<AppEvent> = emptyList()
        override val lastAction: AppEvent? = null
    }
}
