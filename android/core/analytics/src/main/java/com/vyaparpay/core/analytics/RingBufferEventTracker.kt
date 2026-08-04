package com.vyaparpay.core.analytics

import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.asSharedFlow

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
 *
 * **[eventStream] (Phase-4 T7b — see [EventTracker.eventStream]'s kdoc for
 * the gap this closes, and the naming-collision reasoning behind that name).**
 * Backed by a [MutableSharedFlow] fed from [record], deliberately
 * *not* behind the same [lock]: [MutableSharedFlow.tryEmit] is its own
 * lock-free, non-suspending, thread-safe operation, and `record` must never
 * block a caller (main thread or the OkHttp dispatcher) waiting on a slow
 * `ctx.event` collector downstream — the same "never let a slow consumer
 * stall the producer" rule docs/03 §5 states for the screen-state side.
 * `onBufferOverflow = DROP_OLDEST` with a small extra buffer means a
 * catastrophically slow collector drops the *oldest* unconsumed events
 * rather than suspending or crashing `record` — an event lost this way is
 * still in the durable ring buffer and reaches the wire via the next
 * `ctx.snapshot`'s `last_action`/timeline catch-up regardless.
 */
@Singleton
public class RingBufferEventTracker @Inject constructor() : EventTracker {

    private val lock = Any()
    private val buffer = ArrayDeque<AppEvent>(EventTracker.RING_BUFFER_CAPACITY)

    private val _eventStream = MutableSharedFlow<AppEvent>(
        replay = 0,
        extraBufferCapacity = EVENTS_EXTRA_BUFFER_CAPACITY,
        onBufferOverflow = BufferOverflow.DROP_OLDEST,
    )
    override val eventStream: SharedFlow<AppEvent> = _eventStream.asSharedFlow()

    override fun record(event: AppEvent) {
        synchronized(lock) {
            if (buffer.size >= EventTracker.RING_BUFFER_CAPACITY) {
                buffer.removeFirst()
            }
            buffer.addLast(event)
        }
        _eventStream.tryEmit(event)
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

    private companion object {
        private const val EVENTS_EXTRA_BUFFER_CAPACITY = 16
    }
}
