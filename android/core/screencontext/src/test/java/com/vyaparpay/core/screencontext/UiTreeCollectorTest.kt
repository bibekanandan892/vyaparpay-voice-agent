package com.vyaparpay.core.screencontext

import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.snapshots.Snapshot
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [UiTreeCollector]'s own plain-JVM-testable surface: the `start`/`stop`
 * lifecycle. The walk itself (`attachRoot` -> real `RootForTest` ->
 * `SemanticsNode`) has no meaningful fake on a plain JVM — `SemanticsNode`
 * has no public constructor reachable without a real Compose composition —
 * so that path is deliberately NOT unit-tested here. It is proven instead by
 * `UiTreeCollectorPaymentScreenCanaryTest` (`:feature:payments`), a
 * Robolectric test that drives a REAL rendered screen through this class end
 * to end — the "CI canary" docs/07 §2.1's honesty note calls for, so a
 * Compose upgrade that breaks the `RootForTest`/`unmergedRootSemanticsNode`
 * cast fails the build loudly instead of silently. The root-clearing half of
 * [UiTreeCollector.stop]'s contract needs a real root for the same reason, and
 * is proven by `UiTreeCollectorRootLifecycleTest`, also in `:feature:payments`.
 *
 * **The `start()` contract changed in Phase-4 T8e, and the test here pinned
 * the old one.** `start()` used to `check(observerHandle == null)`, and this
 * file asserted "starting twice throws". That was right while the collector
 * died with its Activity; once it was correctly scoped `@Singleton`, that
 * throw became process-global and reachable from ordinary app behaviour:
 * `AndroidCallNotifier.contentIntent()` relaunches the activity `CLEAR_TOP`
 * without `SINGLE_TOP` against a `standard` launchMode, leaving two instances
 * briefly alive at once (measured in `MainActivityOverlappingWindowsTest`). A
 * *configuration change* is NOT such a case — it is strictly
 * destroy-then-create — and an earlier version of this kdoc wrongly said it was. The tests below pin the reference-counted contract
 * that replaced it.
 *
 * They are deliberately behavioural: they observe whether a real Compose
 * snapshot commit still reaches [UiTreeCollector.tree], rather than reading
 * `observerHandle` through a test-only seam. A capture with no roots attached
 * still emits `RawSemanticsTree(emptyList())`, which is exactly the "is the
 * observer alive" signal needed, and needs no `RootForTest`.
 *
 * **The `UnconfinedTestDispatcher` is load-bearing, not incidental** — see
 * [recordCaptures] for what breaks without it. It affects only *when the test's
 * own collector subscribes*, never what the production code does, and the
 * contract is still genuinely proven rather than passed by inlining: reverting
 * the reference counting to plain idempotence fails
 * `the outgoing host's stop leaves the incoming host still observing` and
 * nothing else, and reverting `start()` to its old `check(...)` fails all three
 * tests that start twice. Both were confirmed by mutation.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class UiTreeCollectorTest {

    @Test
    fun `a second host does not throw`() = runTest(UnconfinedTestDispatcher()) {
        val collector = UiTreeCollector(this, debounceMillis = 0)
        val captures = recordCaptures(collector)

        collector.start() // Activity A
        collector.start() // Activity B, created before A is destroyed -- must not throw

        commitAComposeStateChange()
        advanceUntilIdle()

        // Renamed after review: this asserts the second start() does not THROW
        // and that capture still works. It deliberately does NOT claim to detect
        // a second registered observer -- it structurally cannot. Two live
        // observers still yield one capture, because scheduleDebouncedCapture
        // cancels and replaces pendingDebounce and _tree conflates equal values.
        // `the outgoing host's stop leaves the incoming host still observing` is
        // what actually pins the single-registration behaviour, via its
        // consequence.
        assertEquals("one commit must produce one capture", 1, captures.size)
        collector.stop()
        collector.stop()
    }

    @Test
    fun `the outgoing host's stop leaves the incoming host still observing`() = runTest(UnconfinedTestDispatcher()) {
        val collector = UiTreeCollector(this, debounceMillis = 0)
        val captures = recordCaptures(collector)

        collector.start() // Activity A
        collector.start() // Activity B arrives
        collector.stop() // A is destroyed -- B is still live

        commitAComposeStateChange()
        advanceUntilIdle()

        // The regression this guards: making `start()` merely idempotent would
        // let A's `stop()` dispose the observer B depends on, silently ending
        // capture for the rest of the process.
        assertTrue("the observer must survive the outgoing host's stop", captures.isNotEmpty())
        collector.stop()
    }

    @Test
    fun `the last host's stop disposes the observer`() = runTest(UnconfinedTestDispatcher()) {
        val collector = UiTreeCollector(this, debounceMillis = 0)
        val captures = recordCaptures(collector)

        collector.start()
        collector.start()
        collector.stop()
        collector.stop() // the last host leaves

        commitAComposeStateChange()
        advanceUntilIdle()

        assertTrue("no capture may follow the last stop, got ${captures.size}", captures.isEmpty())
    }

    @Test
    fun `stopping before ever starting does not throw`() = runTest(UnconfinedTestDispatcher()) {
        UiTreeCollector(this, debounceMillis = 0).stop() // must not throw
    }

    @Test
    fun `stopping after starting does not throw and allows a later restart`() = runTest(UnconfinedTestDispatcher()) {
        val collector = UiTreeCollector(this, debounceMillis = 0)
        val captures = recordCaptures(collector)

        collector.start()
        collector.stop()
        collector.start() // the ordinary sequential Activity recreation

        commitAComposeStateChange()
        advanceUntilIdle()

        assertTrue("a restarted collector must observe again", captures.isNotEmpty())
        collector.stop()
    }

    /**
     * Collects [UiTreeCollector.tree] for the life of the test. `backgroundScope`
     * is cancelled by `runTest` at the end, so the never-completing collection
     * does not hang the test.
     *
     * These tests run on an `UnconfinedTestDispatcher` specifically so this
     * launch subscribes *eagerly*. Under `StandardTestDispatcher` the collecting
     * coroutine is merely queued, and the capture triggered later in the test is
     * drained before it ever subscribes — every assertion then reads an empty
     * list and the emission only lands as `runTest` tears the background scope
     * down. Found the hard way; the list this returns is only trustworthy
     * because subscription happens before anything can emit.
     */
    private fun TestScope.recordCaptures(collector: UiTreeCollector): List<RawSemanticsTree> {
        val captures = mutableListOf<RawSemanticsTree>()
        backgroundScope.launch { collector.tree.collect { captures += it } }
        return captures
    }

    /**
     * A real Compose state commit — the exact event
     * `Snapshot.registerApplyObserver` exists to hear. `withMutableSnapshot`
     * applies the snapshot and notifies global apply observers synchronously,
     * so this is the production trigger rather than a stand-in for it.
     */
    private fun commitAComposeStateChange() {
        val state = mutableStateOf(0)
        Snapshot.withMutableSnapshot { state.value = state.value + 1 }
    }
}
