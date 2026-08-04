package com.vyaparpay.core.analytics

import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class EventTrackerTest {

    // An implementer overriding nothing but record/recent/lastAction — the
    // shape every existing EventTracker fake across the codebase already
    // has (docs/03 §3.10 gap 2 was resolved additively for exactly this
    // reason: none of them need to change).
    private class MinimalEventTracker : EventTracker {
        private val entries = mutableListOf<AppEvent>()
        override fun record(event: AppEvent) {
            entries += event
        }
        override fun recent(count: Int): List<AppEvent> = entries.takeLast(count).asReversed()
        override val lastAction: AppEvent? get() = entries.lastOrNull()
    }

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

    @Test
    fun `the default events implementation never emits -- an implementer overriding nothing keeps compiling and stays inert`() =
        runTest {
            val tracker: EventTracker = MinimalEventTracker()
            val collected = mutableListOf<AppEvent>()
            backgroundScope.launch { tracker.eventStream.collect { collected += it } }
            runCurrent()

            tracker.record(AppEvent.Tap(name = "pay_now_cta", ts = 1L, screen = "PaymentScreen"))
            runCurrent()

            // The default events (EventTracker.NO_EVENTS) never emits — record()
            // on a MinimalEventTracker only reaches the durable buffer, matching
            // every existing implementer that overrides nothing (judgment call 2).
            assertTrue(collected.isEmpty())
        }
}
