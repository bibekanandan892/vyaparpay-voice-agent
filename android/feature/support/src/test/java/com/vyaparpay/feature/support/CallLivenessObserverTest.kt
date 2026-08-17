package com.vyaparpay.feature.support

import com.vyaparpay.voice.CallState
import com.vyaparpay.voice.EndReason
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [CallLivenessObserver] on its own: the publishing rule (everything but
 * `Ended` is live), that it stops collecting once the call is over, that
 * re-tracking never stacks collectors, and that it behaves under an eager
 * (`Main.immediate`-shaped) dispatcher, where the collector runs inline
 * before `track()` even returns.
 * `CallViewModelTest` covers the integration that matters most — a call
 * ending after its ViewModel was cleared still reaching the signal.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class CallLivenessObserverTest {

    @Test
    fun `publishes live for every state except Ended, then not-live on Ended`() = runTest {
        val signal = CallActivitySignal()
        val observer = CallLivenessObserver(signal, backgroundScope)
        val state = MutableStateFlow<CallState>(CallState.Idle)

        observer.track(state)
        runCurrent()
        // Idle counts as live on purpose: track() is only ever handed a call
        // the ViewModel bound during/after a start, so Idle there is "not yet
        // processed its start intent", not "no call" (kdoc).
        assertTrue(signal.isCallLive.value)

        for (s in listOf(CallState.Requesting, CallState.Signaling, CallState.Connecting, CallState.InCall, CallState.Reconnecting)) {
            state.value = s
            runCurrent()
            assertTrue("$s must read as live", signal.isCallLive.value)
        }

        state.value = CallState.Ended(EndReason.REMOTE_HUNG_UP)
        runCurrent()
        assertFalse(signal.isCallLive.value)
    }

    @Test
    fun `stops collecting once the call has ended`() = runTest {
        val signal = CallActivitySignal()
        val observer = CallLivenessObserver(signal, backgroundScope)
        val state = MutableStateFlow<CallState>(CallState.InCall)
        observer.track(state)
        runCurrent()

        state.value = CallState.Ended(EndReason.USER_HUNG_UP)
        runCurrent()
        assertFalse(signal.isCallLive.value)

        // A finished call's flow is dropped: nothing is subscribed any more,
        // so even an (impossible in production) later emission is not
        // observed. This is what keeps a dead controller from being held.
        assertEquals(0, state.subscriptionCount.value)
    }

    @Test
    fun `re-tracking the same call replaces the collector instead of stacking one`() = runTest {
        // A ViewModel re-binding to a running call (merchant returns to
        // Support mid-call) calls track() again with the same flow.
        val signal = CallActivitySignal()
        val observer = CallLivenessObserver(signal, backgroundScope)
        val state = MutableStateFlow<CallState>(CallState.InCall)

        observer.track(state)
        runCurrent()
        observer.track(state)
        runCurrent()

        assertEquals("exactly one collector, not two", 1, state.subscriptionCount.value)
        assertTrue(signal.isCallLive.value)
    }

    @Test
    fun `tracking a call that is already Ended publishes not-live and holds nothing`() = runTest {
        // Reachable: CallViewModel.handleControllerlessBinding's retry can
        // bind to a controller whose session-mint already failed.
        val signal = CallActivitySignal().apply { update(true) }
        val observer = CallLivenessObserver(signal, backgroundScope)
        val state = MutableStateFlow<CallState>(CallState.Ended(EndReason.SETUP_FAILED))

        observer.track(state)
        runCurrent()

        assertFalse(signal.isCallLive.value)
        assertEquals(0, state.subscriptionCount.value)
    }

    @Test
    fun `abandon publishes not-live and stops collecting a flow that will never end`() = runTest {
        // onServiceDisconnected: the service is gone without a terminal
        // state, so the tracked flow will never emit Ended on its own.
        val signal = CallActivitySignal()
        val observer = CallLivenessObserver(signal, backgroundScope)
        val state = MutableStateFlow<CallState>(CallState.InCall)
        observer.track(state)
        runCurrent()
        assertTrue(signal.isCallLive.value)

        observer.abandon()
        runCurrent()

        assertFalse(signal.isCallLive.value)
        assertEquals(0, state.subscriptionCount.value)
        // And a later, real call is still tracked normally.
        val next = MutableStateFlow<CallState>(CallState.Requesting)
        observer.track(next)
        runCurrent()
        assertTrue(signal.isCallLive.value)
    }

    @Test
    fun `under an eager dispatcher the collector runs inline before track returns`() {
        // Production scope is Dispatchers.Main.immediate: launch{} runs the
        // body inline up to its first suspension, and StateFlow.collect
        // delivers the current value synchronously -- so `signal` is already
        // updated when track() returns, and the self-cancel-on-Ended path
        // runs BEFORE `job = ...` is assigned. UnconfinedTestDispatcher
        // reproduces that eagerness (CallViewModelTest's reentrancy test
        // documents why StandardTestDispatcher cannot).
        val scope = TestScope(UnconfinedTestDispatcher())
        val signal = CallActivitySignal().apply { update(true) }
        val observer = CallLivenessObserver(signal, scope)
        val ended = MutableStateFlow<CallState>(CallState.Ended(EndReason.USER_HUNG_UP))

        observer.track(ended)

        assertFalse(signal.isCallLive.value)
        assertEquals(0, ended.subscriptionCount.value)

        // And a live call tracked afterwards is still observed normally --
        // the early inline Ended must not have wedged the observer.
        val live = MutableStateFlow<CallState>(CallState.InCall)
        observer.track(live)
        assertTrue(signal.isCallLive.value)
        assertEquals(1, live.subscriptionCount.value)
        live.value = CallState.Ended(EndReason.USER_HUNG_UP)
        assertFalse(signal.isCallLive.value)
    }
}
