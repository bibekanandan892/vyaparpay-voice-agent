package com.vyaparpay.feature.payments

import android.view.View
import android.view.ViewGroup
import androidx.activity.ComponentActivity
import androidx.compose.runtime.getValue
import androidx.compose.ui.node.RootForTest
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.vyaparpay.core.screencontext.DropLadder
import com.vyaparpay.core.screencontext.RawSemanticsNode
import com.vyaparpay.core.screencontext.ScreenComponent
import com.vyaparpay.core.screencontext.ScreenComponentRole
import com.vyaparpay.core.screencontext.SemanticSnapshotBuilder
import com.vyaparpay.core.screencontext.UiTreeCollector
import com.vyaparpay.core.screencontext.toJson
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * Phase-4 T5c — the deferred follow-up `SettlementsScreen`'s own review
 * (`d3c7e38`) flagged: `UiTreeCollectorPaymentScreenCanaryTest` proves the
 * capture pipeline against `PaymentScreen`'s five-component screen, but
 * `SettlementsScreen` is the app's one list-at-scale screen (34 seeded
 * batches) and nothing in the suite exercised `SemanticSnapshotBuilder`'s
 * `list`/`list_item` contiguity handling (`fillListVisibleCounts`) or
 * `DropLadder`'s rung-1 list cap against a REAL Robolectric render — only
 * hand-built `RawSemanticsTree` fixtures (`SemanticSnapshotBuilderTest`,
 * `DropLadderTest`) exercised those paths before this test existed. Deferred
 * when `SettlementsScreen` was built because `feat/semantic-snapshot-builder`
 * (T6) was still under independent review at the time; T6 has since merged
 * after three review rounds, unblocking this.
 *
 * Mirrors `UiTreeCollectorPaymentScreenCanaryTest`'s exact pattern and
 * rationale (why this test lives in `:feature:payments` not
 * `:core:screencontext`; why `attachRoot` is called directly rather than via
 * `ViewRootForTest.Companion.onViewCreatedCallback`) — see that file's kdoc
 * for the full reasoning, not repeated here.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34], qualifiers = "w411dp-h891dp") // Robolectric's own default window is too short for this screen's header + weighted list to both fit -- matches SettlementsScreenTest's own config, a normal phone size (Pixel-5-ish).
class SettlementsScreenContextCanaryTest {

    @get:Rule
    val composeTestRule = createAndroidComposeRule<ComponentActivity>()

    @Test
    fun `UiTreeCollector and SemanticSnapshotBuilder handle a real 34-row SettlementsScreen list without corrupting visible_count or exceeding the component cap`() {
        val viewModel = SettlementsViewModel(settlements = SeededSettlementsRepository())

        composeTestRule.setContent {
            val state by viewModel.state.collectAsStateWithLifecycle()
            SettlementsScreen(
                state = state,
                events = viewModel.events,
                onExportCsvClicked = viewModel::onExportCsvClicked,
            )
        }
        composeTestRule.waitForIdle()

        val root = findRootForTest(composeTestRule.activity.window.decorView)
            ?: error(
                "No RootForTest found in the rendered view tree -- the RootForTest " +
                    "cast this canary exists to protect appears to have broken. If " +
                    "this test starts failing after a Compose version bump, that is " +
                    "the signal docs/07 §2.1's honesty note warns about, not a flaky test.",
            )

        val collector = UiTreeCollector(scope = CoroutineScope(Dispatchers.Unconfined))
        collector.attachRoot(root)
        val tree = runBlocking { collector.tree.first() }

        // First proof: the real seeded repository's 34 rows actually reached
        // the composition -- SettlementsViewModel's load() resolved before
        // waitForIdle() returned (a synchronous seeded repository plus
        // Robolectric's coroutine-dispatcher synchronization, the same
        // pattern the PaymentViewModel canary test relies on).
        val testTags = tree.roots.flatMap { allTestTags(it) }
        assertTrue(SETTLEMENTS_LIST_TEST_TAG in testTags)
        assertTrue("settlement_batch_row_0" in testTags)

        val ir = SemanticSnapshotBuilder.build(
            tree = tree,
            screen = "SettlementsScreen",
            flow = "settlements_review",
            recentEvents = emptyList(),
        )

        // Second proof, the highest-value one: total_count reflects every
        // seeded row Compose's CollectionInfo reported (not just what
        // Robolectric's bounded viewport happened to realize), and
        // visible_count -- the field the HIGH-2 review fix exists for --
        // matches what actually survived into the components array, not a
        // stale pre-truncation count.
        val list = ir.components.filterIsInstance<ScreenComponent.ListComponent>().singleOrNull()
        assertTrue("expected a list component, roles were ${ir.components.map { it.role }}", list != null)
        assertEquals(34, list!!.totalCount)
        val survivingItemCount = ir.components.count { it.role == ScreenComponentRole.LIST_ITEM }
        // Guards against a vacuous pass: if the window-size config regressed
        // and Robolectric realized zero rows again, survivingItemCount and
        // visible_count would both trivially be 0 and "match" without this
        // screen's list logic having been exercised at all.
        assertTrue("expected at least one realized row, got $survivingItemCount", survivingItemCount > 0)
        assertEquals(
            "visible_count must match what actually survived into the components array",
            survivingItemCount,
            list.visibleCount,
        )

        // Third proof: the 20-component structural cap held on a real render.
        assertTrue("expected components.size <= 20, was ${ir.components.size}", ir.components.size <= MAX_COMPONENTS)

        // Fourth proof, contiguity itself: every surviving LIST_ITEM sits in
        // one unbroken run immediately after the LIST component -- the exact
        // property fillListVisibleCounts' forward-walk depends on, and the
        // one a per-row status_badge (this screen's own hard constraint,
        // documented in SettlementsScreen.kt's own kdoc) would silently
        // violate. If anything violated it, the contiguous run found here
        // would be shorter than survivingItemCount (a stray LIST_ITEM
        // elsewhere, or a non-LIST_ITEM interrupting the run, both fail this).
        val listIndex = ir.components.indexOf(list)
        val contiguousRunLength = ir.components.drop(listIndex + 1)
            .takeWhile { it.role == ScreenComponentRole.LIST_ITEM }
            .size
        assertEquals(
            "all surviving list_items must form one contiguous run immediately after the list component",
            survivingItemCount,
            contiguousRunLength,
        )

        // Fifth proof: DropLadder's token-budget ladder, run against this
        // real IR's serialized JSON, never leaves the wire frame over
        // budget, and if rung 1 (list-item cap) fired, at most 3 list_items
        // survive in the recompressed output -- DropLadder's own contract,
        // now proven against real-scale data instead of only DropLadderTest's
        // hand-built fixtures.
        val recompressed = DropLadder.recompressOversize(ir.toJson())
        assertTrue(
            "estimated tokens ${recompressed.estimatedTokens} exceeded the ${DropLadder.SNAPSHOT_TOKEN_BUDGET} budget",
            recompressed.estimatedTokens <= DropLadder.SNAPSHOT_TOKEN_BUDGET,
        )
        if (1 in recompressed.droppedRungs) {
            val recompressedItemCount = recompressed.ir["components"]!!.jsonArray
                .count { it.jsonObject["role"]?.jsonPrimitive?.content == "list_item" }
            assertTrue(
                "rung 1 fired but $recompressedItemCount list_items survived (expected <= 3)",
                recompressedItemCount <= 3,
            )
        }
    }

    private fun allTestTags(node: RawSemanticsNode): List<String> =
        listOfNotNull(node.testTag) + node.children.flatMap { allTestTags(it) }

    private fun findRootForTest(view: View): RootForTest? {
        if (view is RootForTest) return view
        if (view is ViewGroup) {
            for (i in 0 until view.childCount) {
                findRootForTest(view.getChildAt(i))?.let { return it }
            }
        }
        return null
    }

    private companion object {
        const val MAX_COMPONENTS = 20
    }
}
