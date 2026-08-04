package com.vyaparpay.feature.payments

import com.vyaparpay.core.network.ApiError
import com.vyaparpay.core.network.ApiResult
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * [OrdersViewModel] state-transition coverage (docs/03 §6's ViewModel-unit-test
 * layer), independent of Compose.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class OrdersViewModelTest {

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
    fun `initial state is loading with no rows before the load coroutine dispatches`() {
        val viewModel = OrdersViewModel(orders = SeededOrdersRepository())

        val state = viewModel.state.value
        assertTrue(state.isLoading)
        assertTrue(state.rows.isEmpty())
    }

    @Test
    fun `the seeded repository yields the canonical soundbox plus two more orders`() = runTest {
        val viewModel = OrdersViewModel(orders = SeededOrdersRepository())
        dispatcher.scheduler.advanceUntilIdle()

        val state = viewModel.state.value
        assertFalse(state.isLoading)
        assertEquals(3, state.totalCount)
        assertEquals(3, state.rows.size)
        assertEquals("ord_snd_0712", state.rows.first().orderId)
        assertTrue(state.rows.first().value.contains("IN_TRANSIT"))
        assertTrue(state.rows.first().value.contains("Delhivery"))
    }

    @Test
    fun `a placed order carries no courier text, since none is seeded for it`() = runTest {
        val viewModel = OrdersViewModel(orders = SeededOrdersRepository())
        dispatcher.scheduler.advanceUntilIdle()

        val placedRow = viewModel.state.value.rows.first { it.orderId == "ord_snd_0725" }
        assertEquals("PLACED", placedRow.value)
    }

    @Test
    fun `a repository failure clears the loading flag without populating rows`() = runTest {
        val viewModel = OrdersViewModel(orders = FailingRepository())
        dispatcher.scheduler.advanceUntilIdle()

        val state = viewModel.state.value
        assertFalse(state.isLoading)
        assertTrue(state.rows.isEmpty())
    }

    private class FailingRepository : OrdersRepository {
        override suspend fun getOrders(limit: Int): ApiResult<OrdersPage> =
            ApiResult.Failure(code = ApiError.UNKNOWN, message = "boom")
    }
}
