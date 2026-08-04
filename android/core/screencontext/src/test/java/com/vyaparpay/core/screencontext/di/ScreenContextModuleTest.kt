package com.vyaparpay.core.screencontext.di

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.setMain
import org.junit.After
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * [ScreenContextModule]'s two judgment calls, proven rather than only
 * documented: the returned scope actually runs work (not a dead/cancelled
 * scope), and a child coroutine's failure does not cancel a sibling
 * (`SupervisorJob`, not a plain `Job`).
 *
 * [Dispatchers.setMain] (from `kotlinx-coroutines-test`, already a test
 * dependency of this module) is what lets a plain-JVM test call
 * [ScreenContextModule.provideCoroutineScope] at all — the real function
 * resolves `Dispatchers.Main.immediate`, which throws "Module with the Main
 * dispatcher is missing" without an installed test Main dispatcher; this is
 * the same reason `PaymentViewModelTest` and friends install one via
 * `Dispatchers.setMain` before touching `viewModelScope`.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class ScreenContextModuleTest {

    private val dispatcher = StandardTestDispatcher()

    @Before
    fun setUp() {
        Dispatchers.setMain(dispatcher)
    }

    @After
    fun tearDown() {
        Dispatchers.resetMain()
    }

    @Test
    fun `provideCoroutineScope returns an active scope that can actually launch work`() {
        val scope = ScreenContextModule.provideCoroutineScope()

        var ran = false
        scope.launch { ran = true }
        dispatcher.scheduler.advanceUntilIdle()

        assertTrue(scope.isActive)
        assertTrue(ran)
    }

    @Test
    fun `provideCoroutineScope survives a child coroutine's failure, per its SupervisorJob`() {
        val scope = ScreenContextModule.provideCoroutineScope()

        var siblingRan = false
        scope.launch { throw IllegalStateException("a UiTreeCollector/AppStateManager bug, simulated") }
        scope.launch { siblingRan = true }
        dispatcher.scheduler.advanceUntilIdle()

        assertTrue(
            "a SupervisorJob must not let one child's failure cancel a sibling sharing this scope",
            siblingRan,
        )
        assertTrue(scope.isActive)
    }
}
