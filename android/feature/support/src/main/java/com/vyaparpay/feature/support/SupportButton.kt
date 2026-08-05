package com.vyaparpay.feature.support

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.unit.dp
import com.vyaparpay.core.analytics.EventTracker
import com.vyaparpay.core.ui.modifier.trackedClickable

/**
 * docs/03-android-architecture.md §3.1's `SupportButton`, given a name and a
 * home of its own.
 *
 * **Judgment call — this is `HelpScreen`'s existing inline CTA extracted, not a
 * second competing affordance.** [HelpScreen]'s kdoc already reconciled docs/03
 * §4 ("`HelpScreen` hosts `SupportButton`") against §3.1's visibility table
 * ("the floating `SupportButton` is hidden on `HelpScreen`"): the floating FAB
 * is deliberately absent here because a floating call button on the screen the
 * merchant *navigated to in order to get help* is redundant chrome, and the
 * real entry point is the in-content CTA. That reasoning did not change when
 * the call flow landed — so this task named the component rather than adding
 * one. `HelpScreen` renders exactly the two CTAs it always did; the only
 * difference is that the primary one is now this shared, named composable
 * instead of a file-private helper, and its `onClick` finally reaches a real
 * call (see [SupportRoute]).
 *
 * **Why the floating, always-available placement is still not built.** §3.1's
 * FAB has to be hosted *above* the `NavHost` so it can float over every
 * operational screen — that host is `:app`'s composition root, which this
 * module does not own. Extracting this composable is precisely what
 * makes that placement a later one-line call site rather than a rewrite: the
 * visibility rule it will need (`route !in AppStateManager.EXCLUDED_ROUTES`)
 * is already computable from `AppStateManager.state`, which this module now
 * injects. Deliberate deferral, not a gap.
 *
 * Built on [Surface] + [Modifier.trackedClickable] rather than Material3's
 * `Button` for the same reason `PaymentScreen`'s `PayNowButton` is (see its
 * kdoc): every tap has to reach the [EventTracker] timeline the agent reads.
 * Always enabled — nothing on this screen gates whether support is reachable —
 * so there is no disabled branch to carry [Role.Button] on; the one branch
 * below attaches it unconditionally.
 */
@Composable
internal fun SupportButton(
    label: String,
    testTag: String,
    filled: Boolean,
    events: EventTracker,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Surface(
        modifier = modifier
            .fillMaxWidth()
            .height(48.dp)
            .trackedClickable(
                events = events,
                screen = SupportDestination.ROUTE,
                testTag = testTag,
                role = Role.Button,
                onClick = onClick,
            ),
        shape = MaterialTheme.shapes.small,
        color = if (filled) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.secondaryContainer,
        contentColor = if (filled) MaterialTheme.colorScheme.onPrimary else MaterialTheme.colorScheme.onSecondaryContainer,
    ) {
        Box(contentAlignment = Alignment.Center, modifier = Modifier.fillMaxSize()) {
            Text(text = label, style = MaterialTheme.typography.labelLarge)
        }
    }
}
