package com.vyaparpay.core.ui.modifier

import androidx.compose.foundation.clickable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.role
import androidx.compose.ui.semantics.semantics
import com.vyaparpay.core.analytics.AppEvent
import com.vyaparpay.core.analytics.EventTracker

/**
 * A clickable that also tells the timeline it was clicked (docs/03 §3.9).
 *
 * The tag is doing double duty on purpose: it is the Compose UI test anchor and
 * the `tap` event's `name` and the IR role source, so a screen that is
 * testable is screen-aware for free — and a screen nobody tagged is visibly not
 * either.
 *
 * @param testTag must follow the docs/03 §4 convention; see `TestTagRoles`.
 * @param role the accessibility semantics role to attach (e.g. [Role.Button]
 *   for a CTA). Review fix (T4.3, PaymentScreen): a plain `clickable {}` with
 *   no role leaves TalkBack announcing the node as a generic clickable region
 *   rather than a button, and diverges from docs/07 §1's own canonical raw-
 *   tree fixture, which documents `pay_now_cta` as carrying `"Role": "Button"`.
 *   Defaults to `null` (no role attached) so existing non-button callers are
 *   unaffected.
 * @param clock defaults to the wall clock; overridable so a test can pin `ts`
 *   without a Compose click actually taking wall-clock time.
 */
public fun Modifier.trackedClickable(
    events: EventTracker,
    screen: String,
    testTag: String,
    role: Role? = null,
    clock: () -> Long = System::currentTimeMillis,
    onClick: () -> Unit,
): Modifier = this
    .testTag(testTag)
    .then(if (role != null) Modifier.semantics { this.role = role } else Modifier)
    .clickable {
        events.record(tapEvent(testTag = testTag, screen = screen, ts = clock()))
        onClick()
    }

/**
 * The `tap` event a click on [testTag] within [screen] produces at [ts].
 *
 * Split out from the `clickable` lambda so the event-shape mapping is
 * unit-testable with plain JUnit — this module has no Compose UI test
 * infrastructure, and the mapping is the only part of [trackedClickable]
 * worth testing in isolation from a real click.
 */
internal fun tapEvent(testTag: String, screen: String, ts: Long): AppEvent.Tap =
    AppEvent.Tap(name = testTag, ts = ts, screen = screen)
