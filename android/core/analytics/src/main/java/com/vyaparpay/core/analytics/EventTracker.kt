package com.vyaparpay.core.analytics

import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.asSharedFlow

/**
 * The user-action timeline (docs/03-android-architecture.md §3.9).
 *
 * A fixed-capacity ring buffer of [AppEvent], in-memory and process-lifetime,
 * feeding both the session-create `recent_events` payload and the in-call
 * `ctx.event` stream. [RingBufferEventTracker] is the real, `@Singleton`
 * implementation — this interface exists so `:core:ui`, `:core:network`, and
 * `:core:screencontext` depend on the *contract*, not the ring-buffer detail,
 * and so a test can substitute a fake without a lock or an `ArrayDeque` in
 * sight.
 *
 * Implementations must be safe to call from any thread: taps and navigation
 * arrive on main, `api_error` arrives on the OkHttp dispatcher.
 */
public interface EventTracker {

    /** Appends [event], evicting the oldest entry once [RING_BUFFER_CAPACITY] is reached. */
    public fun record(event: AppEvent)

    /** Newest-first defensive copy of at most [count] entries. */
    public fun recent(count: Int = DEFAULT_RECENT_COUNT): List<AppEvent>

    /** The most recent entry, or `null` when the timeline is empty. */
    public val lastAction: AppEvent?

    /**
     * **Judgment call (Phase-4 T7b, `ScreenContextPublisher`'s dependency).**
     * docs/03 §3.10/§5 and docs/13 §4 require `ctx.event` frames to publish
     * "immediately, no debounce" — but until this task, [EventTracker] had no
     * way to observe "a new event just landed" independent of a screen/route
     * change: [record]/[recent]/[lastAction] are all pull-only. The original
     * docs/03 §3.11 sketch (`combine(collector.ir, nav.flow, events.stream)`)
     * clearly intended a reactive event source (`events.stream`) that was
     * simply never built.
     *
     * Resolved here as an **additive, default-implemented** member rather
     * than a breaking interface change — with one real collision this
     * surfaced and had to route around: naming this member `events` (the
     * doc sketch's own word) broke compilation in three places this module
     * cannot touch (`DashboardScreenTest`, `PaymentViewModelTest`,
     * `HelpScreenTest`, each in a different `:feature:*` module) because
     * each already declares its own `RecordingEventTracker` test fake with a
     * plain `val events = mutableListOf<AppEvent>()` — Kotlin requires an
     * `override` modifier the moment a subtype member shares a supertype
     * member's name, so an unrelated `val events` became an accidental
     * (and, without a name change, unfixable-from-here) hide. [eventStream]
     * is named to avoid that collision outright rather than working around
     * it token-by-token in files this task must not edit. Every OTHER
     * existing implementer (`InMemoryEventTracker` in `:feature:payments`,
     * and any fake that does not happen to declare a member named
     * `eventStream`) inherits this no-op default and keeps compiling
     * untouched — none of them need real push semantics, since only
     * `ScreenContextPublisher` (via `AppStateManager.events`, a brand-new
     * property on a brand-new class with no such collision risk) reads this.
     * [RingBufferEventTracker], the one production implementation, overrides
     * it with a real [MutableSharedFlow] fed from [record].
     *
     * `replay = 0`: a late subscriber does not need history it missed —
     * [recent] already serves that need for anyone who joins after events
     * happened. This is a hot, "what's happening now" stream, matching the
     * asymmetry `EventTracker`'s own kdoc already draws between the durable
     * ring buffer and a live view of it.
     */
    public val eventStream: SharedFlow<AppEvent> get() = NO_EVENTS

    public companion object {
        /** Ring-buffer capacity fixed by docs/08 §2.2 — ~100 bytes/entry, ~5 KB total. */
        public const val RING_BUFFER_CAPACITY: Int = 50

        /** What `POST /v1/sessions` carries by default (docs/03 §3.11). */
        public const val DEFAULT_RECENT_COUNT: Int = 15

        /**
         * The [eventStream] default: a `SharedFlow` that never emits. A
         * single shared instance (not a fresh one per access) since it is
         * genuinely stateless and immutable — sharing it costs nothing and
         * avoids allocating dead flows on every default-getter call.
         */
        private val NO_EVENTS: SharedFlow<AppEvent> = MutableSharedFlow<AppEvent>(replay = 0).asSharedFlow()
    }
}
