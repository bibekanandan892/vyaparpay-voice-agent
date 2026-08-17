package com.vyaparpay.feature.support

import androidx.activity.ComponentActivity
import androidx.compose.foundation.layout.Row
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Call
import androidx.compose.material3.Icon
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.performClick
import com.vyaparpay.core.analytics.AppEvent
import com.vyaparpay.core.analytics.EventTracker
import com.vyaparpay.core.ui.modifier.trackedClickable
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * Proves the accessibility shape `:app`'s `ReturnToCallChip` uses — a
 * `contentDescription = null` [Icon] and a visible [Text] label as merged
 * siblings *inside* the same [Modifier.trackedClickable] `Surface`'s content
 * — actually produces one merged accessibility node, not two separate
 * TalkBack stops the way the sibling-outside-the-clickable shape
 * `CallControlButton` (`:feature:support`'s own `CallScreen.kt`) had to be
 * fixed away from (that fix is what `clearAndSetSemantics {}` on its own
 * label `Text` exists for).
 *
 * Lives here, not next to `ReturnToCallChip` itself, because `:app` has no
 * working `createAndroidComposeRule<ComponentActivity>()` infrastructure
 * (confirmed empirically — see `ReturnToCallChipTest`'s own kdoc) while this
 * module already does. A generic reproduction of the exact structural shape,
 * not a call into `ReturnToCallChip` across a module boundary it cannot
 * cross.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
class MergedIconTextClickableSemanticsTest {

    @get:Rule
    val composeTestRule = createAndroidComposeRule<ComponentActivity>()

    @Test
    fun `a null-description icon and a text label inside one clickable merge into a single node`() {
        var tapped = false
        val events = RecordingEventTracker()
        composeTestRule.setContent {
            Surface(
                modifier = Modifier.trackedClickable(
                    events = events,
                    screen = "test",
                    testTag = "merged_chip",
                    role = Role.Button,
                    onClick = { tapped = true },
                ),
            ) {
                Row {
                    Icon(imageVector = Icons.Filled.Call, contentDescription = null)
                    Text(text = "Return to call")
                }
            }
        }

        // The discriminating check: one node carrying BOTH the clickable's
        // own testTag AND the label's text content. If the label were not
        // merged into the clickable Surface -- living as an independent,
        // unmerged sibling node instead -- no single node would match both
        // predicates at once, and this lookup would fail to resolve
        // (SemanticsMatcher combines with AND; Compose UI test's default
        // merged-tree traversal is what makes a match here mean "merged",
        // not "coincidentally both present somewhere in the tree").
        val mergedNode = hasTestTag("merged_chip") and hasText("Return to call")
        composeTestRule.onNode(mergedNode).assertIsDisplayed()
        composeTestRule.onNode(mergedNode).performClick()
        assertTrue(tapped)
    }

    private class RecordingEventTracker : EventTracker {
        override fun record(event: AppEvent) = Unit
        override fun recent(count: Int): List<AppEvent> = emptyList()
        override val lastAction: AppEvent? = null
    }
}
