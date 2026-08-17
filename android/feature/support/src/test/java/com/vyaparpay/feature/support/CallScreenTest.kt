package com.vyaparpay.feature.support

import androidx.activity.ComponentActivity
import androidx.compose.runtime.Composable
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.assert
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import com.vyaparpay.core.analytics.AppEvent
import com.vyaparpay.core.analytics.EventTracker
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * [CallScreen] through real Compose semantics, matching [CallStatusPanelTest]'s
 * Robolectric setup.
 *
 * Scope note, same as [CallStatusPanelTest]: this covers mute, speaker,
 * end-call, and the elapsed timer — the surface Phases 1-2 actually wired.
 * There is deliberately no transcript/agent-state assertion here, for the
 * same open-seam reason [CallStatusPanel]'s kdoc gives.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class CallScreenTest {

    @get:Rule
    val composeTestRule = createAndroidComposeRule<ComponentActivity>()

    @Test
    fun `connecting shows a calling status with no timer`() {
        composeTestRule.setContent { Screen(CallUiState(phase = CallPhase.CONNECTING)) }

        composeTestRule.onNodeWithTag(CALL_SCREEN_TEST_TAG).assertIsDisplayed()
        composeTestRule.onNodeWithTag(CALL_SCREEN_STATUS_TEST_TAG).assertIsDisplayed()
        composeTestRule.onNodeWithText("Calling…").assertIsDisplayed()
    }

    @Test
    fun `reconnecting shows a reconnecting status`() {
        composeTestRule.setContent { Screen(CallUiState(phase = CallPhase.RECONNECTING)) }

        composeTestRule.onNodeWithText("Reconnecting…").assertIsDisplayed()
    }

    @Test
    fun `in call with a timer anchor shows connected plus elapsed time`() {
        composeTestRule.setContent {
            Screen(
                CallUiState(phase = CallPhase.IN_CALL, inCallSinceMillis = 10_000L),
                clock = { 10_000L + 65_000L }, // 65s elapsed -> 01:05
            )
        }
        composeTestRule.waitForIdle()

        composeTestRule.onNodeWithText("Connected · 01:05").assertIsDisplayed()
    }

    @Test
    fun `the elapsed timer actually ticks once a second, not a one-shot computation`() {
        // A constant clock lambda (the test above) would pass identically
        // even if rememberElapsedSeconds computed once and never looped —
        // this test drives a mutable clock forward through real coroutine
        // delay(1_000) suspensions via mainClock, so it only passes if the
        // LaunchedEffect's while(true) loop actually re-runs over time.
        composeTestRule.mainClock.autoAdvance = false
        var now = 10_000L
        composeTestRule.setContent {
            Screen(CallUiState(phase = CallPhase.IN_CALL, inCallSinceMillis = 10_000L), clock = { now })
        }
        composeTestRule.mainClock.advanceTimeByFrame()
        composeTestRule.onNodeWithText("Connected · 00:00").assertIsDisplayed()

        now = 11_000L
        composeTestRule.mainClock.advanceTimeBy(1_000L)
        composeTestRule.onNodeWithText("Connected · 00:01").assertIsDisplayed()

        now = 13_000L
        composeTestRule.mainClock.advanceTimeBy(2_000L)
        composeTestRule.onNodeWithText("Connected · 00:03").assertIsDisplayed()
    }

    @Test
    fun `in call with no timer anchor yet shows a bare connected status`() {
        // The narrow, documented cross-thread window where state already
        // reads InCall but inCallSinceMillis hasn't landed yet (Phase 1's
        // CallController.inCallSinceMillis kdoc) — must render something
        // honest, not a stale or negative timer.
        composeTestRule.setContent {
            Screen(CallUiState(phase = CallPhase.IN_CALL, inCallSinceMillis = null))
        }

        composeTestRule.onNodeWithText("Connected").assertIsDisplayed()
    }

    @Test
    fun `all three controls are shown while in call`() {
        composeTestRule.setContent { Screen(CallUiState(phase = CallPhase.IN_CALL)) }

        composeTestRule.onNodeWithTag(CALL_MUTE_TOGGLE_TEST_TAG).assertIsDisplayed()
        composeTestRule.onNodeWithTag(CALL_HANG_UP_TEST_TAG).assertIsDisplayed()
        composeTestRule.onNodeWithTag(CALL_SPEAKER_TOGGLE_TEST_TAG).assertIsDisplayed()
    }

    @Test
    fun `tapping End Call invokes the hang up callback`() {
        var hungUp = false
        composeTestRule.setContent {
            Screen(CallUiState(phase = CallPhase.IN_CALL), onHangUp = { hungUp = true })
        }

        composeTestRule.onNodeWithTag(CALL_HANG_UP_TEST_TAG).performClick()

        assertTrue(hungUp)
    }

    @Test
    fun `tapping mute invokes the toggle callback`() {
        // CallScreen renders CallUiState.muted as given — it does not guess
        // the new state optimistically. The real flip comes back through
        // VoiceCallCoordinator.muted -> CallViewModel -> a new CallUiState.
        var toggled = 0
        composeTestRule.setContent {
            Screen(CallUiState(phase = CallPhase.IN_CALL, muted = false), onToggleMute = { toggled++ })
        }

        composeTestRule.onNodeWithTag(CALL_MUTE_TOGGLE_TEST_TAG).performClick()

        assertEquals(1, toggled)
    }

    @Test
    fun `tapping speaker invokes the toggle callback`() {
        var toggled = 0
        composeTestRule.setContent {
            Screen(CallUiState(phase = CallPhase.IN_CALL), onToggleSpeaker = { toggled++ })
        }

        composeTestRule.onNodeWithTag(CALL_SPEAKER_TOGGLE_TEST_TAG).performClick()

        assertEquals(1, toggled)
    }

    @Test
    fun `a muted state renders the unmute label`() {
        // onNodeWithContentDescription, not onNodeWithText: the visible
        // caption Text carries clearAndSetSemantics {} (see
        // CallControlButton's kdoc — it exists so TalkBack doesn't announce
        // the control twice), so "Unmute" now lives only in the merged
        // Surface node's contentDescription, not as a separately queryable
        // text node.
        composeTestRule.setContent { Screen(CallUiState(phase = CallPhase.IN_CALL, muted = true)) }

        composeTestRule.onNodeWithContentDescription("Unmute").assertIsDisplayed()
    }

    @Test
    fun `an unmuted state renders the mute label`() {
        composeTestRule.setContent { Screen(CallUiState(phase = CallPhase.IN_CALL, muted = false)) }

        composeTestRule.onNodeWithContentDescription("Mute").assertIsDisplayed()
    }

    @Test
    fun `every control records a tap on the shared event timeline`() {
        // Same wiring CallStatusPanelTest proves for End Call — trackedClickable
        // means the mute and speaker taps land on the same timeline too.
        val events = RecordingEventTracker()
        composeTestRule.setContent { Screen(CallUiState(phase = CallPhase.IN_CALL), events = events) }

        composeTestRule.onNodeWithTag(CALL_MUTE_TOGGLE_TEST_TAG).performClick()
        composeTestRule.onNodeWithTag(CALL_SPEAKER_TOGGLE_TEST_TAG).performClick()
        composeTestRule.onNodeWithTag(CALL_HANG_UP_TEST_TAG).performClick()

        assertEquals(3, events.events.size)
        assertTrue(events.events.all { it is AppEvent.Tap })
    }

    // ------------------------------------------------------------------
    // formatElapsed — plain, no Compose/Robolectric needed
    // ------------------------------------------------------------------

    @Test
    fun `formatElapsed pads single-digit minutes and seconds`() {
        assertEquals("00:00", formatElapsed(0L))
        assertEquals("00:05", formatElapsed(5L))
        assertEquals("00:59", formatElapsed(59L))
    }

    @Test
    fun `formatElapsed rolls over at a minute`() {
        assertEquals("01:00", formatElapsed(60L))
        assertEquals("01:05", formatElapsed(65L))
    }

    @Test
    fun `formatElapsed handles a long call past an hour of seconds`() {
        assertEquals("61:05", formatElapsed(3_665L))
    }

    @Test
    fun `a notifications-denied notice is shown DURING the call, not only after it`() {
        // Regression (verification audit, 2026-08-17): PROCEED with
        // notifications denied sets notice=NOTIFICATIONS_DENIED on the way
        // into startCall; the old banner rendered it alongside the live
        // phase, but this screen dropped it, so it surfaced only after
        // "Call ended", where it meant nothing.
        composeTestRule.setContent {
            Screen(CallUiState(phase = CallPhase.CONNECTING, notice = CallNotice.NOTIFICATIONS_DENIED))
        }

        composeTestRule.onNodeWithTag(CALL_SCREEN_NOTICE_TEST_TAG).assertIsDisplayed()
        composeTestRule.onNodeWithText("Notifications are off", substring = true).assertIsDisplayed()
    }

    @Test
    fun `no notice line is composed when there is nothing to say`() {
        composeTestRule.setContent { Screen(CallUiState(phase = CallPhase.IN_CALL)) }

        composeTestRule.onNodeWithTag(CALL_SCREEN_NOTICE_TEST_TAG).assertDoesNotExist()
    }

    @Test
    fun `the speaker toggle exposes its on-off state to accessibility services`() {
        // Mute conveys state through its label flip; Speaker's label is
        // constant, so it must carry a stateDescription or TalkBack reads
        // "Speaker, Button" in both states.
        val speakerOn = androidx.compose.runtime.mutableStateOf(false)
        composeTestRule.setContent {
            Screen(CallUiState(phase = CallPhase.IN_CALL, speakerOn = speakerOn.value))
        }

        composeTestRule.onNodeWithTag(CALL_SPEAKER_TOGGLE_TEST_TAG)
            .assert(SemanticsMatcher.expectValue(SemanticsProperties.StateDescription, "Off"))

        speakerOn.value = true
        composeTestRule.waitForIdle()

        composeTestRule.onNodeWithTag(CALL_SPEAKER_TOGGLE_TEST_TAG)
            .assert(SemanticsMatcher.expectValue(SemanticsProperties.StateDescription, "On"))
    }

    @Composable
    private fun Screen(
        state: CallUiState,
        events: EventTracker = RecordingEventTracker(),
        onHangUp: () -> Unit = {},
        onToggleMute: () -> Unit = {},
        onToggleSpeaker: () -> Unit = {},
        clock: () -> Long = System::currentTimeMillis,
    ) {
        CallScreen(
            state = state,
            events = events,
            onHangUp = onHangUp,
            onToggleMute = onToggleMute,
            onToggleSpeaker = onToggleSpeaker,
            clock = clock,
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
