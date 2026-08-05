package com.vyaparpay.feature.support

import androidx.lifecycle.ViewModelStore
import com.vyaparpay.core.analytics.RingBufferEventTracker
import com.vyaparpay.core.network.SessionCreateRequestDto
import com.vyaparpay.core.screencontext.AppStateManager
import com.vyaparpay.core.screencontext.NavigationTracker
import com.vyaparpay.core.screencontext.UiTreeCollector
import com.vyaparpay.voice.CallState
import com.vyaparpay.voice.EndReason
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import kotlinx.serialization.json.Json
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * [CallViewModel]'s own policy: permission routing, the session request it
 * hands the service, and the service-binding lifecycle.
 *
 * Runs on a plain JVM. [VoiceCallLauncher] and [BoundCall] exist precisely so
 * that is possible — the call *transitions* driven below are already proved
 * against the real reducer by `CallStateMachineTest`/`CallControllerTest` in
 * `:voice`, so what is worth asserting here is what this class does *in
 * response* to them. `CallViewModelSessionBodyTest` covers the one thing a
 * fake genuinely cannot: that the request body comes out of a real, populated
 * [AppStateManager].
 */
@OptIn(ExperimentalCoroutinesApi::class)
class CallViewModelTest {

    private val dispatcher = StandardTestDispatcher()

    @Before
    fun setUp() {
        Dispatchers.setMain(dispatcher)
    }

    @After
    fun tearDown() {
        Dispatchers.resetMain()
    }

    // ---------------------------------------------------------------
    // Permission routing
    // ---------------------------------------------------------------

    @Test
    fun `a granted microphone starts the service and binds to it`() {
        val launcher = FakeVoiceCallLauncher()
        val viewModel = newViewModel(launcher)

        viewModel.onPermissionDecision(proceed())

        assertEquals(1, launcher.started.size)
        // One binding total: the reconnaissance bind from init is still
        // pending, and startCall deliberately reuses it rather than stacking
        // a second connection.
        assertEquals(1, launcher.bindCount)
        assertTrue(launcher.isBound)
        assertEquals(CallPhase.CONNECTING, viewModel.state.value.phase)
    }

    // ---------------------------------------------------------------
    // Reconciliation on creation — the review's HIGH finding
    // ---------------------------------------------------------------

    @Test
    fun `construction binds immediately so an already-running call is rediscovered`() = runTest {
        // HIGH-fix regression (independent review of T8c): the Support route
        // has no state saving, so leaving mid-call destroys the ViewModel
        // while the service keeps the mic open. Before the fix a recreated
        // ViewModel never bound at all — a blank panel over a live call, with
        // no hang-up control. This test fails against that code at connect():
        // the fake throws "connect() called before bind()".
        val launcher = FakeVoiceCallLauncher()
        val viewModel = newViewModel(launcher)

        assertEquals(1, launcher.bindCount)
        launcher.connect(FakeBoundCall(initial = CallState.InCall))
        dispatcher.scheduler.advanceUntilIdle()

        assertEquals(CallPhase.IN_CALL, viewModel.state.value.phase)
        assertTrue(viewModel.state.value.canHangUp)
    }

    @Test
    fun `a reconnaissance bind that finds no call stays idle, never reports an ended call`() {
        // The other half of the HIGH fix: on almost every visit nothing is
        // running, and the probe must resolve to a blank surface — not to
        // "Call ended" for a call that never existed (which is what routing
        // this through the start-in-flight failure path would produce).
        val launcher = FakeVoiceCallLauncher()
        val viewModel = newViewModel(launcher)

        launcher.connect(null)

        assertEquals(CallPhase.IDLE, viewModel.state.value.phase)
        assertFalse(viewModel.state.value.isVisible)
        assertFalse(launcher.isBound)
        assertEquals(1, launcher.unbindCount)
    }

    @Test
    fun `a call can still be started after the reconnaissance probe came back empty`() {
        val launcher = FakeVoiceCallLauncher()
        val viewModel = newViewModel(launcher)
        launcher.connect(null)

        viewModel.startCall()

        assertEquals(1, launcher.started.size)
        assertTrue(launcher.isBound)
        assertEquals(CallPhase.CONNECTING, viewModel.state.value.phase)
    }

    @Test
    fun `a retryable microphone denial shows the retry notice and starts nothing`() {
        val launcher = FakeVoiceCallLauncher()
        val viewModel = newViewModel(launcher)

        viewModel.onPermissionDecision(
            CallPermissionDecision(CallPermissionOutcome.RETRYABLE_DENIAL, notificationsGranted = true),
        )

        assertEquals(CallNotice.MIC_DENIED, viewModel.state.value.notice)
        assertEquals(CallPhase.IDLE, viewModel.state.value.phase)
        assertTrue(launcher.started.isEmpty())
        // No bind beyond init's reconnaissance probe.
        assertEquals(1, launcher.bindCount)
    }

    @Test
    fun `a permanent microphone denial shows the settings notice and starts nothing`() {
        // The branch that must not be a silent no-op: the merchant is told the
        // permission is off for good and offered the settings route out.
        val launcher = FakeVoiceCallLauncher()
        val viewModel = newViewModel(launcher)

        viewModel.onPermissionDecision(
            CallPermissionDecision(CallPermissionOutcome.PERMANENT_DENIAL, notificationsGranted = true),
        )

        assertEquals(CallNotice.MIC_BLOCKED, viewModel.state.value.notice)
        assertEquals(CallPhase.IDLE, viewModel.state.value.phase)
        assertTrue(launcher.started.isEmpty())
    }

    @Test
    fun `denied notifications do not block the call, they only annotate it`() {
        // docs/03 §3.6 and :voice's manifest: "denial degrades to a headless
        // call, never blocks it."
        val launcher = FakeVoiceCallLauncher()
        val viewModel = newViewModel(launcher)

        viewModel.onPermissionDecision(
            CallPermissionDecision(CallPermissionOutcome.PROCEED, notificationsGranted = false),
        )

        assertEquals(1, launcher.started.size)
        assertEquals(CallPhase.CONNECTING, viewModel.state.value.phase)
        assertEquals(CallNotice.NOTIFICATIONS_DENIED, viewModel.state.value.notice)
    }

    @Test
    fun `a fully granted request carries no notice`() {
        val viewModel = newViewModel(FakeVoiceCallLauncher())

        viewModel.onPermissionDecision(proceed())

        assertNull(viewModel.state.value.notice)
    }

    // ---------------------------------------------------------------
    // The session request
    // ---------------------------------------------------------------

    @Test
    fun `the request handed to the service is AppStateManager's session body verbatim`() {
        val appState = newAppStateManager()
        val launcher = FakeVoiceCallLauncher()
        val viewModel = CallViewModel(appState, launcher)

        viewModel.startCall()

        val sent = Json.decodeFromString(SessionCreateRequestDto.serializer(), launcher.started.single())
        assertEquals(appState.sessionCreateBody(CallViewModel.DEMO_USER_ID), sent)
    }

    @Test
    fun `the request carries the seeded demo merchant id`() {
        val launcher = FakeVoiceCallLauncher()
        val viewModel = newViewModel(launcher)

        viewModel.startCall()

        val sent = Json.decodeFromString(SessionCreateRequestDto.serializer(), launcher.started.single())
        // The canonical demo merchant, matching backend/scripts/seed.py's
        // MERCHANT_ID. See CallViewModel.DEMO_USER_ID's kdoc: a stand-in until
        // this app has authentication.
        assertEquals("usr_rajesh01", sent.userId)
        assertEquals("usr_rajesh01", CallViewModel.DEMO_USER_ID)
    }

    @Test
    fun `a second start while a call is in flight is ignored`() {
        // Otherwise a double tap re-sends ACTION_START and drags the UI back
        // to "connecting" over a call that is already up.
        val launcher = FakeVoiceCallLauncher()
        val viewModel = newViewModel(launcher)

        viewModel.startCall()
        viewModel.startCall()

        assertEquals(1, launcher.started.size)
        assertEquals(1, launcher.bindCount)
    }

    @Test
    fun `a call may be started again once the previous one has ended`() = runTest {
        val launcher = FakeVoiceCallLauncher()
        val viewModel = newViewModel(launcher)
        viewModel.startCall()
        val call = FakeBoundCall()
        launcher.connect(call)
        dispatcher.scheduler.advanceUntilIdle()

        call.emit(CallState.Ended(EndReason.USER_HUNG_UP))
        dispatcher.scheduler.advanceUntilIdle()
        viewModel.dismiss()
        viewModel.startCall()

        assertEquals(2, launcher.started.size)
    }

    // ---------------------------------------------------------------
    // Call state observation
    // ---------------------------------------------------------------

    @Test
    fun `the bound call's state drives the UI phase`() = runTest {
        val launcher = FakeVoiceCallLauncher()
        val viewModel = newViewModel(launcher)
        viewModel.startCall()
        val call = FakeBoundCall(initial = CallState.Requesting)
        launcher.connect(call)
        dispatcher.scheduler.advanceUntilIdle()

        assertEquals(CallPhase.CONNECTING, viewModel.state.value.phase)

        call.emit(CallState.InCall)
        dispatcher.scheduler.advanceUntilIdle()
        assertEquals(CallPhase.IN_CALL, viewModel.state.value.phase)

        call.emit(CallState.Reconnecting)
        dispatcher.scheduler.advanceUntilIdle()
        assertEquals(CallPhase.RECONNECTING, viewModel.state.value.phase)

        call.emit(CallState.Ended(EndReason.REMOTE_HUNG_UP))
        dispatcher.scheduler.advanceUntilIdle()
        assertEquals(CallPhase.ENDED, viewModel.state.value.phase)
        assertEquals(EndReason.REMOTE_HUNG_UP, viewModel.state.value.endReason)
    }

    @Test
    fun `a freshly bound controller still reporting Idle stays on connecting`() = runTest {
        // VoiceCallService dispatches SupportTapped through a channel, so the
        // first value this collector sees is routinely CallStateMachine's
        // initial Idle. Reporting IDLE would bounce the UI out of the spinner
        // and re-open the in-flight guard to a second tap.
        val launcher = FakeVoiceCallLauncher()
        val viewModel = newViewModel(launcher)
        viewModel.startCall()

        launcher.connect(FakeBoundCall(initial = CallState.Idle))
        dispatcher.scheduler.advanceUntilIdle()

        assertEquals(CallPhase.CONNECTING, viewModel.state.value.phase)

        viewModel.startCall()
        assertEquals(1, launcher.started.size)
    }

    @Test
    fun `dropped context frames are surfaced from the bound call`() = runTest {
        // CallController.contextFramesDropped's kdoc names this ViewModel as
        // its intended consumer; :voice has no logger, so this is the only way
        // a blind agent becomes visible at all.
        val launcher = FakeVoiceCallLauncher()
        val viewModel = newViewModel(launcher)
        viewModel.startCall()
        val call = FakeBoundCall()
        launcher.connect(call)
        dispatcher.scheduler.advanceUntilIdle()

        call.contextFramesDropped = 7L
        call.emit(CallState.InCall)
        dispatcher.scheduler.advanceUntilIdle()

        assertEquals(7L, viewModel.state.value.contextFramesDropped)
    }

    // ---------------------------------------------------------------
    // Hang up
    // ---------------------------------------------------------------

    @Test
    fun `hanging up reaches the bound call`() = runTest {
        val launcher = FakeVoiceCallLauncher()
        val viewModel = newViewModel(launcher)
        viewModel.startCall()
        val call = FakeBoundCall()
        launcher.connect(call)
        dispatcher.scheduler.advanceUntilIdle()

        viewModel.hangUp()

        assertEquals(1, call.hangUpCount)
    }

    @Test
    fun `a hang up that arrives before the binding is replayed on connect`() = runTest {
        // The narrow but reachable window CallViewModel.pendingHangUp exists
        // for: dropping the tap here would leave a live mic running behind a
        // UI that says the call is over (docs/03 §2.2).
        val launcher = FakeVoiceCallLauncher()
        val viewModel = newViewModel(launcher)
        viewModel.startCall()

        viewModel.hangUp()
        val call = FakeBoundCall()
        launcher.connect(call)
        dispatcher.scheduler.advanceUntilIdle()

        assertEquals(1, call.hangUpCount)
    }

    @Test
    fun `a replayed hang up fires exactly once, not on every later bind`() = runTest {
        val launcher = FakeVoiceCallLauncher()
        val viewModel = newViewModel(launcher)
        viewModel.startCall()
        viewModel.hangUp()
        val first = FakeBoundCall()
        launcher.connect(first)
        dispatcher.scheduler.advanceUntilIdle()

        first.emit(CallState.Ended(EndReason.USER_HUNG_UP))
        dispatcher.scheduler.advanceUntilIdle()
        viewModel.dismiss()
        viewModel.startCall()
        val second = FakeBoundCall()
        launcher.connect(second)
        dispatcher.scheduler.advanceUntilIdle()

        assertEquals(1, first.hangUpCount)
        assertEquals(0, second.hangUpCount)
    }

    // ---------------------------------------------------------------
    // Binding lifecycle — the leak-shaped assertions
    // ---------------------------------------------------------------

    @Test
    fun `the binding is released when the call reaches a terminal state`() = runTest {
        // Load-bearing, not tidiness: VoiceCallService ends a call with
        // stopSelf(), and a bound service is not destroyed by its own stopSelf
        // until every client unbinds. Holding on here keeps the service and
        // its notification alive forever.
        val launcher = FakeVoiceCallLauncher()
        val viewModel = newViewModel(launcher)
        viewModel.startCall()
        val call = FakeBoundCall()
        launcher.connect(call)
        dispatcher.scheduler.advanceUntilIdle()
        assertTrue(launcher.isBound)

        call.emit(CallState.Ended(EndReason.USER_HUNG_UP))
        dispatcher.scheduler.advanceUntilIdle()

        assertFalse(launcher.isBound)
        assertEquals(1, launcher.unbindCount)
    }

    @Test
    fun `clearing the ViewModel releases a live binding`() = runTest {
        // The real teardown path: ViewModelStore.clear() is what the framework
        // calls, and it is what invokes onCleared(). A ServiceConnection that
        // outlives its owner is held strongly by the framework until
        // unbindService — a genuine leak, and one that keeps the bound service
        // alive with it.
        val launcher = FakeVoiceCallLauncher()
        val store = ViewModelStore()
        val viewModel = newViewModel(launcher)
        store.put("call", viewModel)
        viewModel.startCall()
        launcher.connect(FakeBoundCall())
        dispatcher.scheduler.advanceUntilIdle()

        store.clear()

        assertFalse(launcher.isBound)
        assertEquals(1, launcher.unbindCount)
    }

    @Test
    fun `clearing while the reconnaissance probe is still pending releases it exactly once`() {
        // The probe registers a real ServiceConnection; a cleared ViewModel
        // must release it or the framework holds both forever. Exactly once:
        // unbindService on an already-released connection throws.
        val launcher = FakeVoiceCallLauncher()
        val store = ViewModelStore()
        store.put("call", newViewModel(launcher))

        store.clear()

        assertEquals(1, launcher.unbindCount)
        assertFalse(launcher.isBound)
    }

    @Test
    fun `clearing after the probe already resolved empty does not unbind again`() {
        val launcher = FakeVoiceCallLauncher()
        val store = ViewModelStore()
        store.put("call", newViewModel(launcher))
        launcher.connect(null) // probe resolved: released there and then

        store.clear()

        assertEquals(1, launcher.unbindCount)
    }

    @Test
    fun `clearing after the call already ended does not unbind twice`() = runTest {
        val launcher = FakeVoiceCallLauncher()
        val store = ViewModelStore()
        val viewModel = newViewModel(launcher)
        store.put("call", viewModel)
        viewModel.startCall()
        val call = FakeBoundCall()
        launcher.connect(call)
        dispatcher.scheduler.advanceUntilIdle()
        call.emit(CallState.Ended(EndReason.USER_HUNG_UP))
        dispatcher.scheduler.advanceUntilIdle()

        store.clear()

        assertEquals(1, launcher.unbindCount)
    }

    @Test
    fun `a controller missing on first connect keeps the UI on connecting and retries`() = runTest {
        // The race half of handleControllerlessBinding's kdoc: Android makes
        // no ordering promise between onServiceConnected and a concurrently
        // delivered ACTION_START, so the first connect can legitimately find
        // the controller not yet assigned. The pre-review code declared ENDED
        // right here — over a call that was actually starting, mic and all.
        val launcher = FakeVoiceCallLauncher()
        val viewModel = newViewModel(launcher)
        launcher.connect(null) // resolve the reconnaissance probe: nothing running
        viewModel.startCall()

        launcher.connect(null) // service created, ACTION_START not yet processed

        assertEquals(CallPhase.CONNECTING, viewModel.state.value.phase)

        dispatcher.scheduler.advanceUntilIdle() // the retry rebinds
        assertTrue(launcher.isBound)
        val call = FakeBoundCall(initial = CallState.Signaling)
        launcher.connect(call)
        dispatcher.scheduler.advanceUntilIdle()
        call.emit(CallState.InCall)
        dispatcher.scheduler.advanceUntilIdle()

        assertEquals(CallPhase.IN_CALL, viewModel.state.value.phase)
    }

    @Test
    fun `a start whose controller never attaches exhausts its retries and ends honestly`() = runTest {
        // The reject half: a start the service refused (malformed payload /
        // native failure) leaves the controller null forever — and with this
        // client bound via BIND_AUTO_CREATE the service stays alive in that
        // state, so no disconnect will ever break the tie. Exhaustion is the
        // only honest exit; a spinner that never resolves is the alternative.
        val launcher = FakeVoiceCallLauncher()
        val viewModel = newViewModel(launcher)
        launcher.connect(null)
        viewModel.startCall()

        var rounds = 0
        while (launcher.isBound && rounds < 50) {
            launcher.connect(null)
            dispatcher.scheduler.advanceUntilIdle()
            rounds++
        }

        assertEquals(CallViewModel.CONTROLLER_ATTACH_ATTEMPTS, rounds)
        assertEquals(CallPhase.ENDED, viewModel.state.value.phase)
        assertFalse(launcher.isBound)
    }

    @Test
    fun `a hang up queued during the attach retries is delivered once the controller arrives`() = runTest {
        // pendingHangUp must survive the unbind/rebind cycles: the merchant
        // ended a call we could not yet reach, and forgetting that across a
        // retry would leave the call running against an explicit End.
        val launcher = FakeVoiceCallLauncher()
        val viewModel = newViewModel(launcher)
        launcher.connect(null)
        viewModel.startCall()
        launcher.connect(null) // first attach attempt: controller not yet there
        viewModel.hangUp()
        dispatcher.scheduler.advanceUntilIdle() // retry rebinds

        val call = FakeBoundCall()
        launcher.connect(call)
        dispatcher.scheduler.advanceUntilIdle()

        assertEquals(1, call.hangUpCount)
    }

    @Test
    fun `a dead service process ends the call rather than leaving a spinner`() = runTest {
        // docs/03 §7: end the call honestly, never fake a live one.
        val launcher = FakeVoiceCallLauncher()
        val viewModel = newViewModel(launcher)
        viewModel.startCall()
        val call = FakeBoundCall()
        launcher.connect(call)
        dispatcher.scheduler.advanceUntilIdle()
        call.emit(CallState.InCall)
        dispatcher.scheduler.advanceUntilIdle()

        launcher.disconnect()
        dispatcher.scheduler.advanceUntilIdle()

        assertEquals(CallPhase.ENDED, viewModel.state.value.phase)
        assertFalse(launcher.isBound)
    }

    @Test
    fun `dismiss clears a terminal call but refuses to hide a live one`() = runTest {
        val launcher = FakeVoiceCallLauncher()
        val viewModel = newViewModel(launcher)
        viewModel.startCall()
        val call = FakeBoundCall()
        launcher.connect(call)
        dispatcher.scheduler.advanceUntilIdle()
        call.emit(CallState.InCall)
        dispatcher.scheduler.advanceUntilIdle()

        viewModel.dismiss()
        assertEquals(CallPhase.IN_CALL, viewModel.state.value.phase)

        call.emit(CallState.Ended(EndReason.USER_HUNG_UP))
        dispatcher.scheduler.advanceUntilIdle()
        viewModel.dismiss()
        assertEquals(CallUiState(), viewModel.state.value)
    }

    // ---------------------------------------------------------------
    // Fixtures
    // ---------------------------------------------------------------

    private fun proceed() =
        CallPermissionDecision(CallPermissionOutcome.PROCEED, notificationsGranted = true)

    private fun newViewModel(launcher: VoiceCallLauncher) =
        CallViewModel(newAppStateManager(), launcher)

    /**
     * A real [AppStateManager], built through the same `@Inject` constructor
     * Hilt uses — no stand-in, because "the body came from `AppStateManager`"
     * is the thing under test in this file. It has no capture attached here,
     * so it produces its empty-but-honest body;
     * `CallViewModelSessionBodyTest` drives one with a real Compose capture to
     * rule out a fabricated body that merely happens to match the empty shape.
     */
    private fun newAppStateManager(): AppStateManager {
        val scope = CoroutineScope(Dispatchers.Unconfined)
        val events = RingBufferEventTracker()
        return AppStateManager(
            uiTreeCollector = UiTreeCollector(scope),
            navigationTracker = NavigationTracker(events),
            eventTracker = events,
            scope = scope,
        )
    }
}

/**
 * Mirrors [AndroidVoiceCallLauncher]'s own guards exactly — a second `bind()`
 * while bound is ignored, and `unbind()` while unbound is a no-op — so
 * lifecycle assertions here mean the same thing they would against the real
 * one. A fake that counted raw calls instead would report leaks that the
 * production class does not have, or hide ones it does.
 */
internal class FakeVoiceCallLauncher : VoiceCallLauncher {

    val started = mutableListOf<String>()

    var bindCount: Int = 0
        private set

    var unbindCount: Int = 0
        private set

    private var onBound: ((BoundCall?) -> Unit)? = null
    private var onUnbound: (() -> Unit)? = null

    val isBound: Boolean get() = onBound != null

    override fun start(sessionRequestJson: String) {
        started += sessionRequestJson
    }

    override fun bind(onBound: (BoundCall?) -> Unit, onUnbound: () -> Unit) {
        if (this.onBound != null) return
        bindCount++
        this.onBound = onBound
        this.onUnbound = onUnbound
    }

    override fun unbind() {
        if (onBound == null) return
        unbindCount++
        onBound = null
        onUnbound = null
    }

    /** Simulates `onServiceConnected`. */
    fun connect(call: BoundCall?) {
        val callback = checkNotNull(onBound) { "connect() called before bind()" }
        callback(call)
    }

    /** Simulates `onServiceDisconnected` — the service process died. */
    fun disconnect() {
        val callback = checkNotNull(onUnbound) { "disconnect() called before bind()" }
        callback()
    }
}

/** A [BoundCall] whose state the test drives directly. */
internal class FakeBoundCall(initial: CallState = CallState.Requesting) : BoundCall {

    private val _state = MutableStateFlow(initial)
    override val state: StateFlow<CallState> = _state

    override var contextFramesDropped: Long = 0L

    var hangUpCount: Int = 0
        private set

    override fun hangUp() {
        hangUpCount++
    }

    fun emit(next: CallState) {
        _state.value = next
    }
}
