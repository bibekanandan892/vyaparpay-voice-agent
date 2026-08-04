package com.vyaparpay.core.network

import com.vyaparpay.core.analytics.AppEvent
import com.vyaparpay.core.analytics.EventTracker
import org.junit.Assert.assertEquals
import org.junit.Test

class ApiErrorReporterTest {

    private class FakeEventTracker : EventTracker {
        val recorded = mutableListOf<AppEvent>()
        override fun record(event: AppEvent) {
            recorded += event
        }

        override fun recent(count: Int): List<AppEvent> = recorded.takeLast(count).asReversed()
        override val lastAction: AppEvent? get() = recorded.lastOrNull()
    }

    @Test
    fun `report records an api_error event whose name is METHOD path`() {
        val events = FakeEventTracker()
        val reporter = ApiErrorReporter(events, clock = { 1_784_536_440_820L })

        reporter.report(error = ApiError.DAILY_LIMIT_EXCEEDED, method = "POST", path = "/payments", status = 402)

        val event = events.recorded.single() as AppEvent.ApiErrorEvent
        assertEquals("POST /payments", event.name)
        assertEquals(402, event.status)
        assertEquals("DAILY_LIMIT_EXCEEDED", event.code)
        assertEquals(1_784_536_440_820L, event.ts)
    }

    @Test
    fun `report carries the mapped ApiError's own name as code, folding unrecognised codes to UNKNOWN`() {
        val events = FakeEventTracker()
        val reporter = ApiErrorReporter(events)

        reporter.report(error = ApiError.UNKNOWN, method = "GET", path = "/sessions/a1/summary", status = 500)

        assertEquals("UNKNOWN", (events.recorded.single() as AppEvent.ApiErrorEvent).code)
    }
}
