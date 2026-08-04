package com.vyaparpay.core.analytics

/**
 * The five user-action event types the timeline carries.
 *
 * The taxonomy and the full field set are owned by docs/08-context-and-events.md
 * §2; the taxonomy is intentionally closed — a new event type is a protocol
 * version bump, not a config flag (docs/08 §2.1, protocol/schemas/app_event.v1.json).
 * Deliberately absent, and staying absent: keystrokes, entered values (except
 * the non-sensitive [AppEvent.Input.value]), biometric results.
 */
public enum class AppEventType {
    NAV,
    TAP,
    INPUT,
    API_ERROR,
    DIALOG,
}

/**
 * One entry of the `app_event/v1` timeline
 * (docs/08 §2.1, wire authority: `protocol/schemas/app_event.v1.json`).
 *
 * Modeled as a sealed hierarchy rather than one data class with nullable
 * per-type fields. The wire schema itself is a `oneOf` keyed on `type`, and
 * every variant beyond the shared `{type, name, ts}` canon has its own fields
 * that are either *required* (`nav.from`, `api_error.status` + `.code`,
 * `dialog.visible`) or *optional for a documented reason*
 * (`input.value` — present only for non-sensitive fields, docs/08 §2.1) —
 * exactly the shape a sealed hierarchy expresses at compile time and a single
 * bag-of-nullables cannot: a `NAV` event without `from` should not typecheck,
 * and with nullable fields shared across one class it would silently compile.
 * [AppEventType] is already a closed enum by contract ("a new event type is a
 * protocol version bump, not a config flag" — docs/08 §2.1), so the sealed
 * hierarchy is closed for the same reason, and a `when` over it is exhaustive
 * without an `else` branch — the compiler catches a missed variant the moment
 * the taxonomy grows, instead of a silently-wrong nullable-field default.
 *
 * Every variant carries [name] and [ts] (wire canon, docs/08 §2.1). [type] is
 * kept as an explicit discriminator — not derived from `::class` — so a
 * generic consumer (wire serialization, logging, `EventTracker` itself) can
 * read the taxonomy label without a `when` just to get one.
 */
public sealed interface AppEvent {
    public val type: AppEventType
    public val name: String
    public val ts: Long

    /**
     * `nav` — fired by `NavigationTracker`'s destination-changed listener.
     * [name] is the destination route; [from] is the previous route.
     */
    public data class Nav(
        override val name: String,
        override val ts: Long,
        val from: String,
    ) : AppEvent {
        override val type: AppEventType get() = AppEventType.NAV
    }

    /**
     * `tap` — fired by the shared clickable modifier's click hook in
     * `:core:ui`. [name] is the target label, or testTag when the label is
     * empty; [screen] is the route the tap happened on.
     */
    public data class Tap(
        override val name: String,
        override val ts: Long,
        val screen: String,
    ) : AppEvent {
        override val type: AppEventType get() = AppEventType.TAP
    }

    /**
     * `input` — fired on field **commit** only (focus loss or IME done),
     * never per keystroke (docs/08 §2.1, §2.3). [name] is the field
     * testTag/label. [value] is present only for non-sensitive field classes
     * (docs/07 §5 rule 6's classification) and `null` otherwise — the event
     * *omits* the value rather than sending a redaction marker, since "the
     * user edited the PIN field" is already the whole signal (docs/08 §2.1).
     */
    public data class Input(
        override val name: String,
        override val ts: Long,
        val value: String? = null,
    ) : AppEvent {
        override val type: AppEventType get() = AppEventType.INPUT
    }

    /**
     * `api_error` — fired by the `:core:network` interceptor on any non-2xx
     * response. [name] is `"METHOD path"`; [status] is the HTTP status code;
     * [code] is the docs/13 §1.1 error-code vocabulary string (the same
     * string across the HTTP body, this event, and the ScreenContext IR's
     * `last_api.error_code`).
     */
    public data class ApiErrorEvent(
        override val name: String,
        override val ts: Long,
        val status: Int,
        val code: String,
    ) : AppEvent {
        override val type: AppEventType get() = AppEventType.API_ERROR
    }

    /**
     * `dialog` — fired on dialog window attach/detach, the same hook as the
     * docs/07 §2.1 capture bypass. [name] is the dialog title; [visible] is
     * `true` on attach, `false` on detach.
     */
    public data class Dialog(
        override val name: String,
        override val ts: Long,
        val visible: Boolean,
    ) : AppEvent {
        override val type: AppEventType get() = AppEventType.DIALOG
    }
}
