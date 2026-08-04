package com.vyaparpay.core.analytics

import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The real [EventTracker] (docs/03 §3.9, docs/08 §2.2): ring-buffer eviction
 * at [EventTracker.RING_BUFFER_CAPACITY], `recent()`'s defensive copy, and
 * thread-safety under concurrent `record`/`recent` calls from many threads —
 * matching the multi-thread reality (`nav`/`tap`/`input`/`dialog` on main,
 * `api_error` from the OkHttp dispatcher, docs/08 §2.1).
 */
class RingBufferEventTrackerTest {

    private fun tap(n: Int): AppEvent.Tap = AppEvent.Tap(name = "tap_$n", ts = n.toLong(), screen = "PaymentScreen")

    @Test
    fun `a freshly constructed tracker has no last action and an empty recent list`() {
        // Kotlin does not let an override redeclare a default parameter value,
        // so the zero-arg call below only resolves through the EventTracker-
        // typed reference — exactly how every real caller (ApiErrorReporter,
        // trackedClickable) sees this class.
        val tracker: EventTracker = RingBufferEventTracker()

        assertNull(tracker.lastAction)
        assertTrue(tracker.recent().isEmpty())
    }

    @Test
    fun `recording under capacity keeps every entry, newest first`() {
        val tracker = RingBufferEventTracker()
        (1..5).forEach { tracker.record(tap(it)) }

        val recent = tracker.recent(5)

        assertEquals(listOf(5, 4, 3, 2, 1), recent.map { (it as AppEvent.Tap).ts.toInt() })
        assertEquals(tap(5), tracker.lastAction)
    }

    @Test
    fun `recording past capacity evicts the oldest entries, keeping exactly RING_BUFFER_CAPACITY`() {
        val tracker = RingBufferEventTracker()
        val overflow = 5
        (1..EventTracker.RING_BUFFER_CAPACITY + overflow).forEach { tracker.record(tap(it)) }

        val recent = tracker.recent(EventTracker.RING_BUFFER_CAPACITY)

        assertEquals(EventTracker.RING_BUFFER_CAPACITY, recent.size)
        // Newest-first: the very last recorded entry leads.
        assertEquals(EventTracker.RING_BUFFER_CAPACITY + overflow, (recent.first() as AppEvent.Tap).ts.toInt())
        // The oldest `overflow` entries must have been evicted.
        assertTrue(recent.none { (it as AppEvent.Tap).ts <= overflow.toLong() })
    }

    @Test
    fun `recent honors an explicit count and defaults to DEFAULT_RECENT_COUNT`() {
        val tracker: EventTracker = RingBufferEventTracker()
        (1..20).forEach { tracker.record(tap(it)) }

        assertEquals(EventTracker.DEFAULT_RECENT_COUNT, tracker.recent().size)
        assertEquals(3, tracker.recent(3).size)
    }

    @Test
    fun `recent returns a genuine defensive copy — later records never mutate an already-returned list`() {
        val tracker: EventTracker = RingBufferEventTracker()
        tracker.record(tap(1))
        val snapshot = tracker.recent()

        (2..10).forEach { tracker.record(tap(it)) }

        assertEquals(1, snapshot.size)
        assertEquals(tap(1), snapshot.single())
    }

    @Test
    fun `concurrent record and recent calls from many threads never throw or corrupt the buffer`() {
        val tracker = RingBufferEventTracker()
        val threadCount = 16
        val eventsPerThread = 200
        val pool = Executors.newFixedThreadPool(threadCount)
        val start = CountDownLatch(1)
        val done = CountDownLatch(threadCount)
        val failures = ConcurrentLinkedQueue<Throwable>()

        repeat(threadCount) { threadIndex ->
            pool.submit {
                try {
                    start.await()
                    repeat(eventsPerThread) { i ->
                        tracker.record(tap(threadIndex * eventsPerThread + i))
                        // recent() must hand back a list that is safe to fully
                        // iterate even while other threads keep calling record().
                        tracker.recent(EventTracker.RING_BUFFER_CAPACITY).forEach { event -> event.ts }
                    }
                } catch (t: Throwable) {
                    failures += t
                } finally {
                    done.countDown()
                }
            }
        }

        start.countDown()
        assertTrue("stress test threads did not finish in time", done.await(30, TimeUnit.SECONDS))
        pool.shutdown()

        assertTrue("no thread should throw: $failures", failures.isEmpty())
        assertEquals(EventTracker.RING_BUFFER_CAPACITY, tracker.recent(EventTracker.RING_BUFFER_CAPACITY).size)
    }
}
