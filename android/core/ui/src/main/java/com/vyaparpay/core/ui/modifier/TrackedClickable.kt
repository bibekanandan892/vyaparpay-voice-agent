package com.vyaparpay.core.ui.modifier

import androidx.compose.foundation.clickable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import com.vyaparpay.core.analytics.AppEvent
import com.vyaparpay.core.analytics.AppEventType
import com.vyaparpay.core.analytics.EventTracker

/**
 * A clickable that also tells the timeline it was clicked (docs/03 §3.9).
 *
 * The tag is doing double duty on purpose: it is the Compose UI test anchor and
 * the `tap` event's `target` and the IR role source, so a screen that is
 * testable is screen-aware for free — and a screen nobody tagged is visibly not
 * either.
 *
 * @param testTag must follow the docs/03 §4 convention; see `TestTagRoles`.
 */
public fun Modifier.trackedClickable(
    events: EventTracker,
    screen: String,
    testTag: String,
    onClick: () -> Unit,
): Modifier = this
    .testTag(testTag)
    .clickable {
        events.record(
            AppEvent(type = AppEventType.TAP, target = testTag, screen = screen),
        )
        onClick()
    }
