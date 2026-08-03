package com.vyaparpay.core.analytics

/**
 * The five user-action event types the timeline carries.
 *
 * The taxonomy and the full field set are owned by docs/08-context-and-events.md
 * §2; this scaffold carries only the type discriminator plus the fields every
 * type shares. Deliberately absent, and staying absent: keystrokes, entered
 * values, biometric results.
 */
public enum class AppEventType {
    NAV,
    TAP,
    INPUT,
    API_ERROR,
    DIALOG,
}

/**
 * One entry of the `app_event/v1` timeline.
 *
 * @param type which of the five kinds this is.
 * @param target a stable identifier for what was acted on — a route name, a
 *   testTag, an API error code. Never a user-entered value.
 * @param screen the route the event happened on.
 */
public data class AppEvent(
    val type: AppEventType,
    val target: String,
    val screen: String,
)
