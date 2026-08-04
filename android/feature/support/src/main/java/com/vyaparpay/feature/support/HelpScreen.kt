package com.vyaparpay.feature.support

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.unit.dp
import com.vyaparpay.core.analytics.EventTracker
import com.vyaparpay.core.ui.modifier.trackedClickable

/** testTag for the inline "Call Support" entry point -- role `primary_cta` (`_cta`). */
internal const val CALL_SUPPORT_TEST_TAG = "call_support_cta"

/** testTag for the text-chat fallback CTA -- role `secondary_cta` (`_secondary`). */
internal const val TEXT_CHAT_TEST_TAG = "text_chat_secondary"

/** testTag for the FAQ list container. */
private const val FAQ_LIST_TEST_TAG = "faq_list"

/**
 * The support hub's real content (docs/01-product-and-use-case.md §5's
 * `HelpScreen` row: "FAQ, complaint status, Call Support entry point").
 *
 * **Reconciling docs/03's own internal conflict.** docs/03-android-architecture.md
 * §4's screens table says `HelpScreen` "hosts `SupportButton`"; §3.1's
 * visibility-rules table says the floating `SupportButton` is **hidden** on
 * `HelpScreen`. Both are true at once because they describe different
 * things. The floating FAB (§3.1) -- `SupportButton`, `CallViewModel`,
 * `PermissionManager`, `ConversationOverlay`, none of which exists anywhere
 * in the codebase yet -- is hidden here on purpose: a floating call button on
 * the screen the user *reached* to get help would be redundant chrome.
 * `HelpScreen`'s actual call entry point, per docs/01 §5, is the inline
 * "Call Support" CTA rendered below at [CALL_SUPPORT_TEST_TAG] -- an
 * ordinary in-content button, not a FAB. [onCallSupport] is a plain,
 * defaulted-to-no-op callback (matching `PaymentScreen`'s nav-callback
 * convention): wiring it to a real `VoiceCallService` call start is
 * `:app`-level work spanning `:feature:support`, `:voice`, and permission
 * handling that has not landed yet, and building it is out of this task's
 * scope.
 *
 * **No capture-exclusion code lives here, deliberately.**
 * `SupportDestination.IS_CAPTURED == false` is already this module's entire
 * exclusion contract (see that object's kdoc, [HelpViewModel]'s kdoc, and
 * `NavigationTracker`'s own comment on the `HelpScreen -> "support"`
 * route-to-flow row: "that exclusion is ScreenContextPublisher's job, out of
 * scope here"). Enforcing the exclusion -- refusing to overwrite the last
 * *operational* snapshot while the user is on this route -- is a future
 * `ScreenContextPublisher`/`AppStateManager` concern, not a `HelpScreen` one;
 * this screen just needs to exist as a real, working surface.
 * `NavigationTracker` still records a `nav` event for this route, and every
 * `trackedClickable` below still records its own `tap` -- that is correct,
 * since exclusion applies only to the screen-context *snapshot*, never to
 * the event timeline.
 */
@Composable
internal fun HelpScreen(
    state: HelpUiState,
    events: EventTracker,
    onCallSupport: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Box(
        modifier = modifier
            .fillMaxSize()
            .testTag(SupportDestination.ROOT_TEST_TAG),
    ) {
        Scaffold { padding ->
            HelpContent(
                state = state,
                events = events,
                onCallSupport = onCallSupport,
                modifier = Modifier.padding(padding),
            )
        }
    }
}

@Composable
private fun HelpContent(
    state: HelpUiState,
    events: EventTracker,
    onCallSupport: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(
        modifier = modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Text(text = "Help & support", style = MaterialTheme.typography.headlineSmall)

        if (state.isLoading) {
            CircularProgressIndicator(modifier = Modifier.align(Alignment.CenterHorizontally))
        }

        SupportCta(
            label = "Call Support",
            testTag = CALL_SUPPORT_TEST_TAG,
            filled = true,
            events = events,
            onClick = onCallSupport,
        )
        SupportCta(
            label = "Chat with us instead",
            testTag = TEXT_CHAT_TEST_TAG,
            filled = false,
            events = events,
            // No text-chat surface exists yet (docs/03 §3.6 names it as the
            // RECORD_AUDIO-denial degradation target); the tap is still
            // recorded on the timeline per this file's kdoc note that
            // exclusion applies to the snapshot, not the event stream.
            onClick = {},
        )

        Text(text = "Frequently asked questions", style = MaterialTheme.typography.titleMedium)
        FaqList(faqs = state.faqs)

        Text(text = "Your complaints", style = MaterialTheme.typography.titleMedium)
        ComplaintList(complaints = state.complaints)
    }
}

/**
 * A support CTA, built on [Surface] + [Modifier.trackedClickable] rather
 * than Material3's `Button`, for the same reason `PaymentScreen`'s
 * `PayNowButton` is (see its kdoc). Always enabled -- unlike `PaymentScreen`'s
 * Pay Now or `DashboardScreen`'s quick actions, nothing on this screen gates
 * whether support is reachable -- so there is no disabled branch to carry
 * [Role.Button] on; the one branch below attaches it unconditionally.
 */
@Composable
private fun SupportCta(
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

@Composable
private fun FaqList(faqs: List<FaqEntry>, modifier: Modifier = Modifier) {
    Column(
        modifier = modifier
            .fillMaxWidth()
            .testTag(FAQ_LIST_TEST_TAG),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        faqs.forEachIndexed { index, faq ->
            FaqRow(faq = faq, testTag = "faq_item_$index")
        }
    }
}

@Composable
private fun FaqRow(faq: FaqEntry, testTag: String, modifier: Modifier = Modifier) {
    Column(
        modifier = modifier
            .fillMaxWidth()
            .testTag(testTag),
    ) {
        Text(text = faq.question, style = MaterialTheme.typography.bodyLarge)
        Text(text = faq.answer, style = MaterialTheme.typography.bodyMedium)
    }
}

@Composable
private fun ComplaintList(complaints: List<ComplaintSummary>, modifier: Modifier = Modifier) {
    Column(
        modifier = modifier.fillMaxWidth(),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        complaints.forEachIndexed { index, complaint ->
            ComplaintRow(complaint = complaint, testTag = "complaint_row_$index")
        }
    }
}

@Composable
private fun ComplaintRow(complaint: ComplaintSummary, testTag: String, modifier: Modifier = Modifier) {
    Row(
        modifier = modifier
            .fillMaxWidth()
            .testTag(testTag),
        horizontalArrangement = Arrangement.SpaceBetween,
    ) {
        Text(text = complaint.subject, style = MaterialTheme.typography.bodyMedium)
        Text(text = complaint.status, style = MaterialTheme.typography.bodySmall)
    }
}
