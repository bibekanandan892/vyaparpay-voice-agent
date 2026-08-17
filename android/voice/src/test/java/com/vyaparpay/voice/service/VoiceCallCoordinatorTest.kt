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

    private fun TestScope.coordinator(
        state: MutableStateFlow<CallState>,
        inCallSinceMillis: () -> Long? = { null },
    ): VoiceCallCoordinator =
        VoiceCallCoordinator(
            callState = state,
            audioSession = audioSession,
            notifier = notifier,
            scope = backgroundScope,
            setMuted = { mutedCalls += it },
            hangUp = { hangUpCallCount++ },
            onForegroundServiceRequired = { foregroundRequiredCount++ },
            onCallEnded = { callEndedCount++ },
            inCallSinceMillis = inCallSinceMillis,
        )

    // ------------------------------------------------------------------
    // start() idempotency — load-bearing now that LocalBinder hands the
    // coordinator itself to a bound client, not just VoiceCallService
    // ------------------------------------------------------------------

    @Test
    fun `a second start call is a no-op, not a second set of collectors`() = runTest {
        val state = MutableStateFlow<CallState>(CallState.Idle)
        val coordinator = coordinator(state)
        coordinator.start()
        coordinator.start()
        runCurrent()

        state.value = CallState.Requesting
        runCurrent()

        // Focus/foreground are de-duplicated by `everForegrounded` even with
        // two collectors, so those counters cannot tell one collector from
        // two. What a duplicated collectCallState() genuinely doubles is the
        // per-emission notification push -- one collector, one push per
        // state -- so that is the load-bearing assertion (verified to read 2
        // with the AtomicBoolean guard removed).
        assertEquals(1, audioSession.acquireCallCount)
        assertEquals(1, foregroundRequiredCount)
        assertEquals(1, notifier.shown.size)

        state.value = CallState.Ended(EndReason.SETUP_FAILED)
        runCurrent()

        // Likewise the terminal path: two collectors would clear the
        // notification, release audio and report call-ended twice each.
        assertEquals(1, notifier.clearCallCount)
        assertEquals(1, audioSession.releaseCallCount)
        assertEquals(1, callEndedCount)
    }

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

    // ------------------------------------------------------------------
    // Notification duration anchor (docs/03 §3.3's "live call duration")
    // ------------------------------------------------------------------

    @Test
    fun `the coordinator passes inCallSinceMillis through unconditionally, without gating on phase itself`() = runTest {
        // CallController is what actually withholds the value until the call
        // connects (its own inCallSinceMillis kdoc), and AndroidCallNotifier
        // is what decides whether to render a chronometer from it -- this
        // class's only job is to read the lambda fresh on every push and
        // forward it, not to reinterpret what it means.
        val state = MutableStateFlow<CallState>(CallState.Requesting)
        coordinator(state, inCallSinceMillis = { 42_000L }).start()
        runCurrent()

        assertEquals(42_000L, notifier.shown.last().inCallSinceMillis)
    }

    @Test
    fun `the duration anchor reaches the notification once in call`() = runTest {
        val state = MutableStateFlow<CallState>(CallState.InCall)
        coordinator(state, inCallSinceMillis = { 7_000L }).start()
        runCurrent()

        assertEquals(7_000L, notifier.shown.last().inCallSinceMillis)
    }

    @Test
    fun `the duration anchor stays stable across a mute toggle`() = runTest {
        // pushNotification() fires on every mute-driven update too
        // (applyMuteState) -- the anchor must not flicker or reset just
        // because the mute button was tapped.
        val state = MutableStateFlow<CallState>(CallState.InCall)
        val coordinator = coordinator(state, inCallSinceMillis = { 7_000L })
        coordinator.start()
        runCurrent()

        coordinator.toggleMute()
        runCurrent()

        assertEquals(7_000L, notifier.shown.last().inCallSinceMillis)
        assertTrue("every pushed state carried the same anchor", notifier.shown.all { it.inCallSinceMillis == 7_000L })
    }

    @Test
    fun `a null duration anchor is the default when the caller supplies none`() = runTest {
        val state = MutableStateFlow<CallState>(CallState.InCall)
        coordinator(state).start() // default inCallSinceMillis = { null }
        runCurrent()

        assertEquals(null, notifier.shown.last().inCallSinceMillis)
    }

    // ------------------------------------------------------------------
    // Observable state for a bound UI: muted / audioRoute
    // ------------------------------------------------------------------

    @Test
    fun `muted starts false and reflects a user toggle`() = runTest {
        val state = MutableStateFlow<CallState>(CallState.InCall)
        val coordinator = coordinator(state)
        coordinator.start()
        runCurrent()

        assertEquals(false, coordinator.muted.value)

        coordinator.toggleMute()
        runCurrent()

        assertEquals(true, coordinator.muted.value)
    }

    @Test
    fun `muted reflects auto-mute the same way the notification does`() = runTest {
        val state = MutableStateFlow<CallState>(CallState.InCall)
        val coordinator = coordinator(state)
        coordinator.start()
        runCurrent()

        audioSession.focusChangesFlow.emit(AudioFocusChange.LOST_TRANSIENT)
        runCurrent()
        assertEquals(true, coordinator.muted.value)

        audioSession.focusChangesFlow.emit(AudioFocusChange.GAINED)
        runCurrent()
        assertEquals(false, coordinator.muted.value)
    }

    @Test
    fun `audioRoute defaults to earpiece before any call, matching what acquire hard-sets on Requesting`() = runTest {
        val state = MutableStateFlow<CallState>(CallState.Idle)
        val coordinator = coordinator(state)
        coordinator.start()
        runCurrent()

        assertEquals(AudioRoute.EARPIECE, coordinator.audioRoute.value)
        assertEquals(0, audioSession.acquireCallCount)

        // The default is not just a constructor guess: acquire() itself is
        // what actually hard-sets EARPIECE in production
        // (AndroidCallAudioSession.acquire), and this drives the state that
        // triggers it to confirm the two stay consistent.
        state.value = CallState.Requesting
        runCurrent()

        assertEquals(1, audioSession.acquireCallCount)
        assertEquals(AudioRoute.EARPIECE, coordinator.audioRoute.value)
    }

    @Test
    fun `toggling speaker switches to SPEAKER and calls setRoute`() = runTest {
        val state = MutableStateFlow<CallState>(CallState.InCall)
        val coordinator = coordinator(state)
        coordinator.start()
        runCurrent()

        coordinator.toggleSpeaker()
        runCurrent()

        assertEquals(AudioRoute.SPEAKER, coordinator.audioRoute.value)
        assertEquals(listOf(AudioRoute.SPEAKER), audioSession.routes)
    }

    @Test
    fun `toggling speaker twice returns to earpiece`() = runTest {
        val state = MutableStateFlow<CallState>(CallState.InCall)
        val coordinator = coordinator(state)
        coordinator.start()
        runCurrent()

        coordinator.toggleSpeaker()
        coordinator.toggleSpeaker()
        runCurrent()

        assertEquals(AudioRoute.EARPIECE, coordinator.audioRoute.value)
        assertEquals(listOf(AudioRoute.SPEAKER, AudioRoute.EARPIECE), audioSession.routes)
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
