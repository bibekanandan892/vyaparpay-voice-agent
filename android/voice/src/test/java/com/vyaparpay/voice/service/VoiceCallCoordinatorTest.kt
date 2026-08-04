package com.vyaparpay.voice.service

import com.vyaparpay.voice.CallState
import com.vyaparpay.voice.EndReason
import com.vyaparpay.voice.audio.AudioFocusChange
import com.vyaparpay.voice.audio.AudioRoute
import com.vyaparpay.voice.audio.CallAudioSession
import com.vyaparpay.voice.notification.CallNotificationPhase
import com.vyaparpay.voice.notification.CallNotificationState
import com.vyaparpay.voice.notification.CallNotifier
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [VoiceCallCoordinator] against fakes behind [CallAudioSession]/[CallNotifier]
 * — no `AudioManager`, no `NotificationManager`, no service, matching the
 * `CallControllerTest` seam pattern one layer up.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class VoiceCallCoordinatorTest {

    private val audioSession = FakeCallAudioSession()
    private val notifier = FakeCallNotifier()
    private val mutedCalls = mutableListOf<Boolean>()
    private var hangUpCallCount = 0
    private var foregroundRequiredCount = 0
    private var callEndedCount = 0

    private fun TestScope.coordinator(state: MutableStateFlow<CallState>): VoiceCallCoordinator =
        VoiceCallCoordinator(
            callState = state,
            audioSession = audioSession,
            notifier = notifier,
            scope = backgroundScope,
            setMuted = { mutedCalls += it },
            hangUp = { hangUpCallCount++ },
            onForegroundServiceRequired = { foregroundRequiredCount++ },
            onCallEnded = { callEndedCount++ },
        )

    // ------------------------------------------------------------------
    // Audio focus: requested before capture, abandoned exactly once
    // ------------------------------------------------------------------

    @Test
    fun `focus is requested the moment the service must be in the foreground, before Signaling opens the mic`() = runTest {
        val state = MutableStateFlow<CallState>(CallState.Idle)
        coordinator(state).start()
        runCurrent()
        assertEquals(0, audioSession.acquireCallCount)

        state.value = CallState.Requesting
        runCurrent()

        // Requesting is where requiresForegroundService first turns true
        // (docs/03 CallState.kt) and strictly precedes Signaling, the
        // transition where CallController's OpenTransport effect calls
        // WebRtcClient.start() and the mic track is created. Acquiring here
        // — not at InCall — is what makes focus/MODE_IN_COMMUNICATION land
        // before capture begins.
        assertEquals(1, audioSession.acquireCallCount)
        assertEquals(1, foregroundRequiredCount)
    }

    @Test
    fun `focus is acquired exactly once across a full happy path`() = runTest {
        val state = MutableStateFlow<CallState>(CallState.Idle)
        coordinator(state).start()
        runCurrent()

        for (next in listOf(
            CallState.Requesting,
            CallState.Signaling,
            CallState.Connecting,
            CallState.InCall,
        )) {
            state.value = next
            runCurrent()
        }

        assertEquals(1, audioSession.acquireCallCount)
        assertEquals(1, foregroundRequiredCount)
    }

    @Test
    fun `a refused focus grant does not stop the call from proceeding`() = runTest {
        audioSession.acquireReturns = false
        val state = MutableStateFlow<CallState>(CallState.Idle)
        coordinator(state).start()
        runCurrent()

        state.value = CallState.Requesting
        runCurrent()

        assertEquals(1, audioSession.acquireCallCount)
        assertEquals(0, hangUpCallCount)
        assertEquals(0, callEndedCount)
    }

    @Test
    fun `focus is abandoned exactly once when setup fails before InCall`() = runTest {
        val state = MutableStateFlow<CallState>(CallState.Idle)
        coordinator(state).start()
        runCurrent()

        state.value = CallState.Requesting
        runCurrent()
        state.value = CallState.Ended(EndReason.SETUP_FAILED)
        runCurrent()

        assertEquals(1, audioSession.acquireCallCount)
        assertEquals(1, audioSession.releaseCallCount)
        assertEquals(1, callEndedCount)
        assertEquals(1, notifier.clearCallCount)
    }

    @Test
    fun `focus is abandoned exactly once on a user hang-up from InCall`() = runTest {
        val state = MutableStateFlow<CallState>(CallState.InCall)
        coordinator(state).start()
        runCurrent()

        state.value = CallState.Ended(EndReason.USER_HUNG_UP)
        runCurrent()

        assertEquals(1, audioSession.releaseCallCount)
        assertEquals(1, callEndedCount)
    }

    @Test
    fun `focus is abandoned exactly once on a remote bye from InCall`() = runTest {
        val state = MutableStateFlow<CallState>(CallState.InCall)
        coordinator(state).start()
        runCurrent()

        state.value = CallState.Ended(EndReason.REMOTE_HUNG_UP)
        runCurrent()

        assertEquals(1, audioSession.releaseCallCount)
        assertEquals(1, callEndedCount)
    }

    @Test
    fun `focus is abandoned exactly once when the reconnect grace expires`() = runTest {
        val state = MutableStateFlow<CallState>(CallState.InCall)
        coordinator(state).start()
        runCurrent()

        state.value = CallState.Reconnecting
        runCurrent()
        state.value = CallState.Ended(EndReason.GRACE_EXPIRED)
        runCurrent()

        assertEquals(1, audioSession.releaseCallCount)
        assertEquals(1, callEndedCount)
    }

    // ------------------------------------------------------------------
    // Service must not outlive a terminal CallState
    // ------------------------------------------------------------------

    @Test
    fun `the service is asked to stop exactly once, only on reaching a terminal state`() = runTest {
        val state = MutableStateFlow<CallState>(CallState.Idle)
        coordinator(state).start()
        runCurrent()

        for (next in listOf(CallState.Requesting, CallState.Signaling, CallState.Connecting, CallState.InCall)) {
            state.value = next
            runCurrent()
            assertEquals("stop requested before a terminal state", 0, callEndedCount)
        }

        state.value = CallState.Ended(EndReason.USER_HUNG_UP)
        runCurrent()

        assertEquals(1, callEndedCount)
    }

    // ------------------------------------------------------------------
    // Transient vs permanent focus loss
    // ------------------------------------------------------------------

    @Test
    fun `a transient focus loss mutes the uplink without ending the call`() = runTest {
        val state = MutableStateFlow<CallState>(CallState.InCall)
        coordinator(state).start()
        runCurrent()

        audioSession.focusChangesFlow.emit(AudioFocusChange.LOST_TRANSIENT)
        runCurrent()

        assertEquals(listOf(true), mutedCalls)
        assertEquals(0, hangUpCallCount)
        assertEquals(0, callEndedCount)
    }

    @Test
    fun `regaining focus after a transient loss unmutes the uplink`() = runTest {
        val state = MutableStateFlow<CallState>(CallState.InCall)
        coordinator(state).start()
        runCurrent()

        audioSession.focusChangesFlow.emit(AudioFocusChange.LOST_TRANSIENT)
        runCurrent()
        audioSession.focusChangesFlow.emit(AudioFocusChange.GAINED)
        runCurrent()

        assertEquals(listOf(true, false), mutedCalls)
    }

    @Test
    fun `a duck request mutes instead of ducking the volume`() = runTest {
        val state = MutableStateFlow<CallState>(CallState.InCall)
        coordinator(state).start()
        runCurrent()

        audioSession.focusChangesFlow.emit(AudioFocusChange.DUCK_REQUESTED)
        runCurrent()

        assertEquals(listOf(true), mutedCalls)
    }

    @Test
    fun `a permanent focus loss ends the call instead of muting`() = runTest {
        val state = MutableStateFlow<CallState>(CallState.InCall)
        coordinator(state).start()
        runCurrent()

        audioSession.focusChangesFlow.emit(AudioFocusChange.LOST)
        runCurrent()

        assertEquals(1, hangUpCallCount)
        assertTrue("a permanent loss must not go through the auto-mute path", mutedCalls.isEmpty())
    }

    @Test
    fun `a user mute survives a transient focus loss and regain`() = runTest {
        val state = MutableStateFlow<CallState>(CallState.InCall)
        val coordinator = coordinator(state)
        coordinator.start()
        runCurrent()

        coordinator.toggleMute() // user mutes
        runCurrent()
        audioSession.focusChangesFlow.emit(AudioFocusChange.LOST_TRANSIENT)
        runCurrent()
        audioSession.focusChangesFlow.emit(AudioFocusChange.GAINED)
        runCurrent()

        // The last effective mute state must still be true: gaining focus
        // back only lifts the auto-mute half, never a user's own mute.
        assertEquals(true, mutedCalls.last())
    }

    @Test
    fun `toggling mute twice returns to unmuted`() = runTest {
        val state = MutableStateFlow<CallState>(CallState.InCall)
        val coordinator = coordinator(state)
        coordinator.start()
        runCurrent()

        coordinator.toggleMute()
        coordinator.toggleMute()
        runCurrent()

        assertEquals(listOf(true, false), mutedCalls)
    }

    // ------------------------------------------------------------------
    // Notification lifecycle
    // ------------------------------------------------------------------

    @Test
    fun `the notification tracks each phase and is cleared exactly once at the end`() = runTest {
        val state = MutableStateFlow<CallState>(CallState.Idle)
        coordinator(state).start()
        runCurrent()

        for (next in listOf(
            CallState.Requesting,
            CallState.Signaling,
            CallState.Connecting,
            CallState.InCall,
            CallState.Reconnecting,
            CallState.InCall,
        )) {
            state.value = next
            runCurrent()
        }
        state.value = CallState.Ended(EndReason.USER_HUNG_UP)
        runCurrent()

        assertEquals(
            listOf(
                CallNotificationPhase.CONNECTING, // Requesting
                CallNotificationPhase.CONNECTING, // Signaling
                CallNotificationPhase.CONNECTING, // Connecting
                CallNotificationPhase.IN_CALL,
                CallNotificationPhase.RECONNECTING,
                CallNotificationPhase.IN_CALL,
            ),
            notifier.shown.map { it.phase },
        )
        assertEquals(1, notifier.clearCallCount)
    }

    @Test
    fun `no notification is ever shown while the call is still Idle`() = runTest {
        val state = MutableStateFlow<CallState>(CallState.Idle)
        coordinator(state).start()
        runCurrent()

        assertTrue(notifier.shown.isEmpty())
        assertEquals(0, notifier.clearCallCount)
    }

    @Test
    fun `mute toggles are reflected in the next notification update`() = runTest {
        val state = MutableStateFlow<CallState>(CallState.InCall)
        val coordinator = coordinator(state)
        coordinator.start()
        runCurrent()

        coordinator.toggleMute()
        runCurrent()

        assertEquals(true, notifier.shown.last().muted)
    }
}

// ------------------------------------------------------------------
// Fakes
// ------------------------------------------------------------------

private class FakeCallAudioSession : CallAudioSession {

    val focusChangesFlow = MutableSharedFlow<AudioFocusChange>(extraBufferCapacity = FOCUS_BUFFER)
    override val focusChanges: SharedFlow<AudioFocusChange> = focusChangesFlow

    var acquireReturns: Boolean = true
    var acquireCallCount: Int = 0
        private set
    var releaseCallCount: Int = 0
        private set
    val routes: MutableList<AudioRoute> = mutableListOf()

    override fun acquire(): Boolean {
        acquireCallCount++
        return acquireReturns
    }

    override fun release() {
        releaseCallCount++
    }

    override fun setRoute(route: AudioRoute) {
        routes += route
    }

    private companion object {
        private const val FOCUS_BUFFER = 8
    }
}

private class FakeCallNotifier : CallNotifier {

    val shown: MutableList<CallNotificationState> = mutableListOf()
    var clearCallCount: Int = 0
        private set

    override fun show(state: CallNotificationState) {
        shown += state
    }

    override fun clear() {
        clearCallCount++
    }
}
