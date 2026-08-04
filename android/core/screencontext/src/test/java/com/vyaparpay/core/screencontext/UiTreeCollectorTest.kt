package com.vyaparpay.core.screencontext

import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestScope
import org.junit.Assert.assertThrows
import org.junit.Test

/**
 * [UiTreeCollector]'s own plain-JVM-testable surface: the `start`/`stop`
 * lifecycle guards. The walk itself (`attachRoot` -> real `RootForTest` ->
 * `SemanticsNode`) has no meaningful fake on a plain JVM — `SemanticsNode`
 * has no public constructor reachable without a real Compose composition —
 * so that path is deliberately NOT unit-tested here. It is proven instead by
 * `UiTreeCollectorPaymentScreenCanaryTest` (`:feature:payments`), a
 * Robolectric test that drives a REAL rendered screen through this class end
 * to end — the "CI canary" docs/07 §2.1's honesty note calls for, so a
 * Compose upgrade that breaks the `RootForTest`/`unmergedRootSemanticsNode`
 * cast fails the build loudly instead of silently.
 */
class UiTreeCollectorTest {

    @Test
    fun `starting twice throws`() {
        val scope = TestScope(StandardTestDispatcher())
        val collector = UiTreeCollector(scope)

        collector.start()

        assertThrows(IllegalStateException::class.java) { collector.start() }
    }

    @Test
    fun `stopping before ever starting does not throw`() {
        val scope = TestScope(StandardTestDispatcher())
        val collector = UiTreeCollector(scope)

        collector.stop() // must not throw
    }

    @Test
    fun `stopping after starting does not throw and allows a later restart`() {
        val scope = TestScope(StandardTestDispatcher())
        val collector = UiTreeCollector(scope)

        collector.start()
        collector.stop()
        collector.start() // observer handle was cleared by stop(); a restart must be allowed
    }
}
