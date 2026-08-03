package com.vyaparpay.core.analytics

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class EventTrackerTest {

    @Test
    fun `ring buffer capacity matches the timeline budget in docs 08 section 2`() {
        assertEquals(50, EventTracker.RING_BUFFER_CAPACITY)
    }

    @Test
    fun `session create carries fewer events than the buffer holds`() {
        assertTrue(EventTracker.DEFAULT_RECENT_COUNT < EventTracker.RING_BUFFER_CAPACITY)
    }

    @Test
    fun `the taxonomy is exactly the five types docs 08 defines`() {
        assertEquals(
            listOf(
                AppEventType.NAV,
                AppEventType.TAP,
                AppEventType.INPUT,
                AppEventType.API_ERROR,
                AppEventType.DIALOG,
            ),
            AppEventType.entries.toList(),
        )
    }
}
