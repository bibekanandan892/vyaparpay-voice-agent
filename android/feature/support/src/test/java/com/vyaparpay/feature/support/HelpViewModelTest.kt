package com.vyaparpay.feature.support

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
 * [HelpViewModel] state-transition coverage (docs/03-android-architecture.md
 * §6): the load-on-construction happy path seeding the canonical
 * `cmp_0724_0031` complaint (docs/13-api-contracts.md §3.4), independent of
 * Compose.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class HelpViewModelTest {

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
    fun `initial state is loading with no faqs or complaints resolved yet`() {
        val viewModel = HelpViewModel(support = SeededSupportRepository())

        val state = viewModel.state.value

        assertTrue(state.isLoading)
        assertTrue(state.faqs.isEmpty())
        assertTrue(state.complaints.isEmpty())
    }

    @Test
    fun `a successful load populates the seeded FAQs and the canonical complaint`() = runTest {
        val viewModel = HelpViewModel(support = SeededSupportRepository())

        dispatcher.scheduler.advanceUntilIdle()

        val state = viewModel.state.value
        assertFalse(state.isLoading)
        assertTrue(state.faqs.isNotEmpty())
        val canonical = state.complaints.first { it.complaintId == "cmp_0724_0031" }
        assertEquals("T+1 settlement not credited", canonical.subject)
        assertEquals("open", canonical.status)
    }

    @Test
    fun `a failed load clears the loading flag without crashing`() = runTest {
        val viewModel = HelpViewModel(
            support = object : SupportRepository {
                override suspend fun getHelpContent() = ApiResult.Failure(
                    code = ApiError.INTERNAL,
                    message = "boom",
                )
            },
        )

        dispatcher.scheduler.advanceUntilIdle()

        assertFalse(viewModel.state.value.isLoading)
    }
}
