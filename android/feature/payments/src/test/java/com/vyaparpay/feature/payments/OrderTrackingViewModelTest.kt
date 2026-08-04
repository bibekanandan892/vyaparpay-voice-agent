package com.vyaparpay.feature.payments

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Before
import org.junit.Test

/**
 * [OrderTrackingViewModel.selectOrder] coverage (docs/03 §6's
 * ViewModel-unit-test layer) -- review fix: the original version had no way
 * to be told which order to track at all. `StandardTestDispatcher`'s manual
 * scheduler lets this test drive both orderings of the real race
 * [selectOrder]'s kdoc describes: `selectOrder` called before the initial
 * load resolves, and after.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class OrderTrackingViewModelTest {

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
    fun `with no selection, the first IN_TRANSIT order is tracked by default`() = runTest {
        val viewModel = OrderTrackingViewModel(orders = SeededOrdersRepository())
        dispatcher.scheduler.advanceUntilIdle()

        assertEquals("In transit", viewModel.state.value.statusValue)
    }

    @Test
    fun `selectOrder called after the load resolves switches to the requested order`() = runTest {
        val viewModel = OrderTrackingViewModel(orders = SeededOrdersRepository())
        dispatcher.scheduler.advanceUntilIdle() // the initial load resolves first

        viewModel.selectOrder("ord_qr_0705") // DELIVERED, not the default IN_TRANSIT order

        assertEquals("Delivered", viewModel.state.value.statusValue)
    }

    @Test
    fun `selectOrder called before the load resolves is applied once it does`() = runTest {
        val viewModel = OrderTrackingViewModel(orders = SeededOrdersRepository())

        viewModel.selectOrder("ord_snd_0725") // PLACED -- called before advanceUntilIdle, i.e. before init's coroutine has run at all
        dispatcher.scheduler.advanceUntilIdle()

        assertEquals("Placed", viewModel.state.value.statusValue)
    }

    @Test
    fun `selectOrder with an unknown id falls back to the default IN_TRANSIT order`() = runTest {
        val viewModel = OrderTrackingViewModel(orders = SeededOrdersRepository())
        dispatcher.scheduler.advanceUntilIdle()

        viewModel.selectOrder("ord_does_not_exist")

        assertEquals("In transit", viewModel.state.value.statusValue)
    }
}
