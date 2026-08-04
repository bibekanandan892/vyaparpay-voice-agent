package com.vyaparpay.feature.dashboard

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
 * [DashboardViewModel] state-transition coverage (docs/03-android-architecture.md
 * §6): the load-on-construction happy path, and a failed load mapping onto
 * the screen's one `alert_banner` slot, independent of Compose.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class DashboardViewModelTest {

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
    fun `initial state is loading with no balances resolved yet`() {
        val viewModel = DashboardViewModel(dashboard = SeededDashboardRepository())

        val state = viewModel.state.value

        assertTrue(state.isLoading)
        assertEquals(0L, state.walletBalancePaise)
        assertEquals(0L, state.collectionsTodayPaise)
        assertEquals("", state.settlementEtaLabel)
    }

    @Test
    fun `a successful load carries the seeded wallet balance from backend seed`() = runTest {
        val viewModel = DashboardViewModel(dashboard = SeededDashboardRepository())

        dispatcher.scheduler.advanceUntilIdle()

        val state = viewModel.state.value
        assertFalse(state.isLoading)
        assertEquals(SeededDashboardRepository.WALLET_BALANCE_PAISE, state.walletBalancePaise)
        assertEquals(SeededDashboardRepository.COLLECTIONS_TODAY_PAISE, state.collectionsTodayPaise)
        assertEquals(SeededDashboardRepository.SETTLEMENT_ETA_LABEL, state.settlementEtaLabel)
        assertNotNull(state.alertMessage)
    }

    @Test
    fun `a failed load surfaces the failure message as the alert banner`() = runTest {
        val viewModel = DashboardViewModel(
            dashboard = object : DashboardRepository {
                override suspend fun getDashboard() = ApiResult.Failure(
                    code = ApiError.INTERNAL,
                    message = "Couldn't load your dashboard right now.",
                )
            },
        )

        dispatcher.scheduler.advanceUntilIdle()

        val state = viewModel.state.value
        assertFalse(state.isLoading)
        assertEquals("Couldn't load your dashboard right now.", state.alertMessage)
    }
}
