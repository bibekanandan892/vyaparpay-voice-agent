package com.vyaparpay.feature.payments

import com.vyaparpay.core.analytics.AppEvent
import com.vyaparpay.core.analytics.EventTracker

/**
 * A correct, minimal [EventTracker], originally added as [PaymentViewModel]'s
 * (and this module's other ViewModels') constructor default before a shared
 * `@Singleton` `EventTracker` existed anywhere in the app.
 *
 * **Reconciled (Phase-4 T8a).** That gap is closed now: `:core:analytics`'s
 * `AnalyticsModule` binds the real `RingBufferEventTracker` singleton, and
 * every `@HiltViewModel` in this module (`PaymentViewModel`,
 * `SettlementsViewModel`, `OrdersViewModel`, `OrderTrackingViewModel`) now
 * has an `@Inject` constructor that resolves it from Hilt — production code
 * (every `XxxRoute` composable, via `hiltViewModel()`) never touches this
 * class anymore. It remains as those same ViewModels' constructor DEFAULT
 * (only reachable via direct, non-DI construction) and as an explicit fake in
 * this module's own tests (`UiTreeCollectorPaymentScreenCanaryTest`,
 * `SettlementsScreenContextCanaryTest`, and others construct it directly) —
 * both legitimate, `:core:analytics`-independent uses for a small,
 * self-contained fake, matching the ring-buffer contract exactly rather than
 * differing from it.
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
