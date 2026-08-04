package com.vyaparpay.core.analytics

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

    public companion object {
        /** Ring-buffer capacity fixed by docs/08 §2.2 — ~100 bytes/entry, ~5 KB total. */
        public const val RING_BUFFER_CAPACITY: Int = 50

        /** What `POST /v1/sessions` carries by default (docs/03 §3.11). */
        public const val DEFAULT_RECENT_COUNT: Int = 15
    }
}
