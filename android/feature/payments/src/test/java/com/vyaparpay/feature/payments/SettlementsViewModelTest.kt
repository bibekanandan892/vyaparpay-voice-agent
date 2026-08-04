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
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * [SettlementsViewModel] state-transition coverage (docs/03-android-architecture.md
 * §6's ViewModel-unit-test layer), independent of Compose -- the load-on-init
 * happy path, the deterministic-seed guarantee this task's brief requires,
 * and the failure path.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class SettlementsViewModelTest {

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
        val viewModel = SettlementsViewModel(settlements = SeededSettlementsRepository())

        val state = viewModel.state.value

        assertTrue(state.isLoading)
        assertTrue(state.rows.isEmpty())
        assertEquals(0, state.totalCount)
    }

    @Test
    fun `loading the seeded repository yields all 34 batches, canonical-latest first`() = runTest {
        val viewModel = SettlementsViewModel(settlements = SeededSettlementsRepository())
        dispatcher.scheduler.advanceUntilIdle()

        val state = viewModel.state.value
        assertFalse(state.isLoading)
        assertEquals(34, state.totalCount)
        assertEquals(34, state.rows.size)
        assertEquals("setl_0723_t1", state.rows.first().settlementId)
        assertEquals("setl_0722_t1", state.rows[1].settlementId)
    }

    @Test
    fun `the canonical latest batch drives the header balance and status fields`() = runTest {
        val viewModel = SettlementsViewModel(settlements = SeededSettlementsRepository())
        dispatcher.scheduler.advanceUntilIdle()

        val state = viewModel.state.value
        assertEquals("SETTLED", state.latestBatchStatus)
        assertTrue("expected a rupee-formatted net figure, was '${state.netSettledValue}'", state.netSettledValue.startsWith("₹"))
        assertTrue("expected a rupee-formatted fees figure, was '${state.feesValue}'", state.feesValue.startsWith("₹"))
    }

    @Test
    fun `the deterministic seed produces at least one shortfall, so the alert banner has something to report`() = runTest {
        val viewModel = SettlementsViewModel(settlements = SeededSettlementsRepository())
        dispatcher.scheduler.advanceUntilIdle()

        assertNotNull(viewModel.state.value.shortfallMessage)
    }

    @Test
    fun `two fresh loads of the seeded repository agree byte-for-byte -- pure function of index, no Random or wall clock`() = runTest {
        val first = SettlementsViewModel(settlements = SeededSettlementsRepository())
        val second = SettlementsViewModel(settlements = SeededSettlementsRepository())
        dispatcher.scheduler.advanceUntilIdle()

        assertEquals(first.state.value.rows, second.state.value.rows)
        assertEquals(first.state.value.netSettledValue, second.state.value.netSettledValue)
    }

    @Test
    fun `every generated row satisfies net equals gross minus fees`() = runTest {
        val viewModel = SettlementsViewModel(settlements = SeededSettlementsRepository())
        dispatcher.scheduler.advanceUntilIdle()

        // SettlementsRepository.getSettlements only returns rows; re-derive
        // from the same repository directly to check the raw invariant
        // (Settlement's own `init` block already enforces this at
        // construction time -- this test guards against that check ever
        // being weakened without anyone noticing).
        val page = (SeededSettlementsRepository().getSettlements(100) as ApiResult.Success).data
        page.items.forEach { settlement ->
            assertEquals(settlement.netPaise, settlement.grossPaise - settlement.feesPaise)
        }
    }

    @Test
    fun `a repository failure clears the loading flag without populating rows`() = runTest {
        val viewModel = SettlementsViewModel(settlements = FailingRepository())
        dispatcher.scheduler.advanceUntilIdle()

        val state = viewModel.state.value
        assertFalse(state.isLoading)
        assertTrue(state.rows.isEmpty())
    }

    private class FailingRepository : SettlementsRepository {
        override suspend fun getSettlements(limit: Int): ApiResult<SettlementsPage> =
            ApiResult.Failure(code = ApiError.UNKNOWN, message = "boom")
    }
}
