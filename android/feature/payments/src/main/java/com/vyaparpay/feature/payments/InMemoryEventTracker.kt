package com.vyaparpay.feature.payments

import com.vyaparpay.core.analytics.AppEvent
import com.vyaparpay.core.analytics.EventTracker

/**
 * A correct, minimal [EventTracker] used as [PaymentViewModel]'s default.
 *
 * `:core:analytics` ships [EventTracker] as an interface only — its
 * `@Singleton` ring-buffer implementation and Hilt binding are explicitly
 * scoped to land with the context pipeline (see the KDoc on
 * [EventTracker]), and `:app`'s Hilt graph does not provide one yet. Rather
 * than block this screen on that future work, or fabricate a shared
 * `@Singleton` here (which would collide with that work when it lands), this
 * is a screen-local implementation of the full documented contract —
 * fixed-capacity ring buffer, synchronized, defensive copies — so it behaves
 * identically to whatever the eventual shared instance does. Swapping it for
 * the real app-scoped singleton later is a one-line constructor-argument
 * change, not a rewrite.
 */
public class InMemoryEventTracker : EventTracker {

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

    override fun recent(count: Int): List<AppEvent> = synchronized(lock) {
        buffer.asReversed().take(count).toList()
    }

    override val lastAction: AppEvent?
        get() = synchronized(lock) { buffer.lastOrNull() }
}
