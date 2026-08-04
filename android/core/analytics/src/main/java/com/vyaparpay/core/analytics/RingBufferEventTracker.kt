package com.vyaparpay.core.analytics

import javax.inject.Inject
import javax.inject.Singleton

/**
 * The real [EventTracker]: a fixed-capacity ring buffer of [AppEvent],
 * in-memory and process-lifetime (docs/03 §3.9, docs/08 §2.2).
 *
 * **Thread-safety.** Appends arrive from more than one thread — `nav`/`tap`/
 * `input`/`dialog` on main, `api_error` from the OkHttp dispatcher — so the
 * buffer is a plain [ArrayDeque] guarded by a single monitor: [record] and
 * [recent] are both `synchronized`, and [recent] copies the buffer into a new
 * `List` before returning it so a caller iterating the result never observes
 * a concurrent mutation. At ~100 bytes/entry the whole buffer is ~5 KB —
 * docs/08 §2.2 is explicit this does not need to be lock-free, and a
 * lock-free structure here would be complexity with no measurable payoff.
 */
@Singleton
public class RingBufferEventTracker @Inject constructor() : EventTracker {

    private val lock = Any()
    private val buffer = ArrayDeque<AppEvent>(EventTracker.RING_BUFFER_CAPACITY)

    override fun record(event: AppEvent) {
        synchronized(lock) {
            if (buffer.size >= EventTracker.RING_BUFFER_CAPACITY) {
                buffer.removeFirst()
            }
            buffer.addLast(event)
        }
    }

    override fun recent(count: Int): List<AppEvent> {
        synchronized(lock) {
            // asReversed() gives newest-first; take(count) + toList() copies
            // eagerly into a fresh ArrayList, which is the defensive copy —
            // the returned list shares no backing storage with [buffer].
            return buffer.asReversed().take(count).toList()
        }
    }

    override val lastAction: AppEvent?
        get() = synchronized(lock) { buffer.lastOrNull() }
}
