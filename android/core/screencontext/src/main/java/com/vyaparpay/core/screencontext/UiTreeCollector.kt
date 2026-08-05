package com.vyaparpay.core.screencontext

import androidx.compose.runtime.snapshots.ObserverHandle
import androidx.compose.runtime.snapshots.Snapshot
import androidx.compose.ui.geometry.Rect
import androidx.compose.ui.node.RootForTest
import androidx.compose.ui.semantics.CollectionInfo
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.SemanticsActions
import androidx.compose.ui.semantics.SemanticsConfiguration
import androidx.compose.ui.semantics.SemanticsNode
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.semantics.SemanticsPropertyKey
import androidx.compose.ui.state.ToggleableState
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.filterNotNull
import kotlinx.coroutines.launch

/**
 * Reads the live Compose semantics tree per docs/07-ui-semantic-context.md
 * §2.1 and reduces it to a framework-free [RawSemanticsTree] the (plain-JVM
 * testable) [SemanticSnapshotBuilder] consumes.
 *
 * **Honesty note (docs/07 §2.1, verbatim in spirit):** [RootForTest] is
 * test-oriented API — literally named for it, and originally exposed so
 * Compose's own testing framework can reach `semanticsOwner` without an
 * accessibility service — used here in production. That is a deliberate,
 * documented risk, not an oversight: there is no blessed non-accessibility
 * API for reading Compose's unmerged semantics tree in-process (see docs/07
 * §2.3's rejected alternatives — `AccessibilityService` has the wrong
 * permission model and trust scope for a fintech app; a screenshot+vision
 * pipeline is strictly worse on privacy, cost, and latency; server-side
 * compression of a raw dump ships unredacted screen content off-device). The
 * production evolution of this component is the same code plus a CI canary
 * that fails loudly the moment a Compose upgrade breaks the cast — that
 * canary is `UiTreeCollectorPaymentScreenCanaryTest` in `:feature:payments`
 * (this module cannot host it itself: `:core:screencontext` has no
 * production dependency on any real screen to walk, and a `:core:* ->
 * :feature:*` edge would violate docs/03 §1's module dependency rule even
 * for a test — see that test's own kdoc).
 *
 * **Thread confinement — pushed to the caller, not solved here.** The walk
 * reads live [SemanticsNode] state, which is only valid from the thread that
 * owns the composition (docs/07 §2.1: "the semantics tree must be read [on
 * the UI thread]"). Rather than dispatch onto `Dispatchers.Main` internally —
 * which would pull `kotlinx-coroutines-android` into this module for a
 * concern the caller already owns — this class follows the same convention
 * `VoiceCallCoordinator` (`:voice`) documents: **the caller must supply a
 * [scope] backed by a dispatcher confined to the UI thread.** Every method
 * below (`attachRoot`, `detachRoot`, the snapshot-apply-driven debounce) runs
 * its actual work inside a `scope.launch { }` block, so as long as [scope]'s
 * dispatcher is UI-confined, the walk itself always executes there too.
 *
 * **Capture cadence.** [start] marks the tree dirty on
 * `Snapshot.registerApplyObserver` — fired on every Compose state commit,
 * process-wide, matching the doc's own description ("commits" are a global
 * concept in the snapshot system, not scoped to one composition) — and
 * schedules a walk debounced [DEBOUNCE_MILLIS] trailing-edge, collapsing a
 * burst of recompositions (e.g. one per keystroke) into one capture. Two
 * paths bypass the debounce for an immediate capture: [attachRoot] (a new
 * window — most importantly a dialog — attaching) and [forceImmediateCapture]
 * (exposed for a future `NavigationTracker`-driven publisher task to call on
 * destination change, per docs/07 §2.1; wiring that call is out of this
 * task's scope — see `NavigationTracker`'s own kdoc for why). [detachRoot]
 * (a dialog dismissing) deliberately does NOT bypass: docs/07 §2.1 names
 * exactly two bypass triggers, both about *new* information the agent must
 * not miss, and 300 ms of staleness on a window closing is imperceptible —
 * a documented judgment call, not an oversight.
 *
 * **Multi-window tracking.** [attachRoot]/[detachRoot] are plain public
 * methods, not wired to Compose's `ViewRootForTest.Companion.onViewCreatedCallback`
 * internally. That callback is a **process-wide singleton `var`** — Compose's
 * own test infrastructure (`createAndroidComposeRule` and friends) already
 * claims it to build its own root registry, and a second writer clobbers
 * whichever one registered second. Wiring this class to that callback
 * directly would make it fight the compose-ui-test framework the moment both
 * are active in the same process (exactly the situation
 * `UiTreeCollectorPaymentScreenCanaryTest` is in). Exposing `attachRoot`/
 * `detachRoot` as plain methods — the caller decides how it discovers roots,
 * whether via that callback in production or a direct reference in a test —
 * sidesteps that conflict entirely and keeps this class honestly scoped to
 * "given a root, walk it," matching `NavigationTracker.bind`'s own
 * discovery-is-the-caller's-job precedent.
 *
 * **Resolved (Phase-4 T8a): the caller is `MainActivity`.** It walks
 * `window.decorView`'s `ViewGroup` tree for a `RootForTest` instance -- the
 * exact `findRootForTest` technique `UiTreeCollectorPaymentScreenCanaryTest`/
 * `SettlementsScreenContextCanaryTest` (`:feature:payments`) already use
 * against a test-only Compose tree, now applied to the app's real one --
 * posted via `window.decorView.post { }` so the walk happens after
 * `setContent {}`'s Compose tree has actually been created (verified
 * empirically against a Robolectric-rendered `MainActivity`, not assumed --
 * see `MainActivityScreenContextTest` in `:app`). See `MainActivity`'s own
 * kdoc for the full lifecycle wiring (`start`/`stop` against
 * `onCreate`/`onDestroy`).
 *
 * **`@Singleton` (Phase-4 T8e) — its absence was a silent, total outage.**
 * This class has two injection points: `MainActivity`'s field (the instance
 * that gets [start], [attachRoot] and a `ChildWindowTracker`) and
 * `AppStateManager`'s `@Inject` constructor. Unscoped, Hilt built one for each,
 * so `AppStateManager` combined over a collector that was never started and had
 * no root attached. Its `_tree` stayed `null`, `combine` waits for every source,
 * and so `AppStateManager.state` never advanced past its `AppContextState()`
 * seed — `sessionCreateBody` shipped `screen_context: null`, and
 * `ScreenContextPublisher.publishScreenState`'s `state.screen ?: return`
 * returned every time: **no `ctx.snapshot` or `ctx.delta` was ever published, on
 * any call.** Only `ctx.event` frames flowed, off the genuinely-`@Singleton`
 * `RingBufferEventTracker`.
 *
 * Nothing could see it: every unit test builds its own collector directly, and
 * `MainActivityScreenContextTest` asserts only against
 * `activity.uiTreeCollector` — the one instance that did work. Meanwhile
 * `MainActivity.kt:110`, `ChildWindowTracker.kt:151` and
 * `MainActivityDialogWindowCaptureTest.kt:161` each already described this class
 * as an app-lifetime `@Singleton`, the last crediting `ScreenContextModule` with
 * a binding it never had. `FullChainScreenContextCanaryTest` (`:app`) is the
 * regression test — the first to read `AppStateManager` and `MainActivity` in
 * the same breath, and all four of its cases fail without this annotation.
 *
 * @param scope UI-thread-confined; see the threading contract above.
 * @param debounceMillis docs/07 §2.1's 300 ms trailing-edge debounce, no longer defaulted -- see the [@Inject] secondary constructor's own kdoc for why.
 */
@Singleton
public class UiTreeCollector(
    private val scope: CoroutineScope,
    private val debounceMillis: Long,
) {

    /**
     * **Judgment call (Phase-4 T8a) -- why `@Inject` lands on a SECOND
     * constructor, not the primary one above.** Dagger/Hilt does not honor
     * Kotlin default parameter values on an `@Inject`-annotated constructor --
     * every declared parameter becomes a required binding regardless of any
     * default (a well-documented Dagger limitation, not an oversight here).
     * Annotating the primary constructor directly would therefore force
     * `ScreenContextModule` (`:core:screencontext`'s `di` package) or some
     * other module to provide an unqualified `Long` -- a binding with no
     * meaningful owner that would silently collide with any other
     * unqualified `Long` the graph ever grows.
     *
     * The primary constructor above therefore lost `debounceMillis`'s
     * default (the one behavior-preserving-but-source-breaking change this
     * task makes to this file), and this `@Inject` constructor -- taking only
     * [scope], the one dependency `ScreenContextModule` can meaningfully
     * provide -- supplies docs/07 §2.1's 300 ms constant directly,
     * reproducing the old default exactly. `UiTreeCollectorPaymentScreenCanaryTest`'s
     * and `SettlementsScreenContextCanaryTest`'s existing
     * `UiTreeCollector(scope = CoroutineScope(Dispatchers.Unconfined))` call
     * sites now resolve to *this* constructor (single named `scope` argument)
     * instead of the two-arg primary -- same runtime behavior, zero test
     * changes needed (confirmed by running both canaries unmodified).
     */
    @Inject
    public constructor(scope: CoroutineScope) : this(scope, DEBOUNCE_MILLIS)

    private val attachedRoots = mutableListOf<RootForTest>()
    private var observerHandle: ObserverHandle? = null
    private var pendingDebounce: Job? = null

    private val _tree = MutableStateFlow<RawSemanticsTree?>(null)

    /** The latest walked tree. Emits nothing until the first capture completes. */
    public val tree: Flow<RawSemanticsTree> = _tree.asStateFlow().filterNotNull()

    /** Begins observing Compose state commits. Call once, on the UI thread. */
    public fun start() {
        check(observerHandle == null) { "UiTreeCollector.start() called twice" }
        observerHandle = Snapshot.registerApplyObserver { _, _ -> scheduleDebouncedCapture() }
    }

    /** Stops observing and cancels any pending debounced capture. */
    public fun stop() {
        observerHandle?.dispose()
        observerHandle = null
        scope.launch { pendingDebounce?.cancel() }
    }

    /**
     * A new Compose window attached (docs/07 §2.1's dialog-attach bypass —
     * also covers the very first, non-dialog attach, since there is no
     * earlier data to prefer over an immediate first capture).
     */
    public fun attachRoot(root: RootForTest) {
        scope.launch {
            attachedRoots += root
            pendingDebounce?.cancel()
            captureNow()
        }
    }

    /** A Compose window detached (e.g. a dialog dismissed) — debounced, not immediate; see class kdoc. */
    public fun detachRoot(root: RootForTest) {
        scope.launch {
            attachedRoots -= root
        }
        scheduleDebouncedCapture()
    }

    /** The debounce bypass docs/07 §2.1 reserves for navigation destination changes. */
    public fun forceImmediateCapture() {
        scope.launch {
            pendingDebounce?.cancel()
            captureNow()
        }
    }

    private fun scheduleDebouncedCapture() {
        scope.launch {
            pendingDebounce?.cancel()
            pendingDebounce = launch {
                delay(debounceMillis)
                captureNow()
            }
        }
    }

    private fun captureNow() {
        _tree.value = RawSemanticsTree(attachedRoots.toList().map(::walkRoot))
    }

    // -- The walk: real SemanticsNode -> framework-free RawSemanticsNode ----

    private fun walkRoot(root: RootForTest): RawSemanticsNode {
        val rootNode = root.semanticsOwner.unmergedRootSemanticsNode
        return toRawNode(rootNode, rootBounds = rootNode.boundsInRoot)
    }

    private fun toRawNode(node: SemanticsNode, rootBounds: Rect): RawSemanticsNode {
        val config = node.config
        val bounds = node.boundsInRoot
        // Rule 1's geometry-based prune criteria, reduced to booleans here —
        // no `Rect` ever leaves this function (see RawSemanticsTree.kt's
        // file kdoc for why bounds must not cross into the IR-adjacent type).
        val zeroSize = bounds.isEmpty
        val offscreen = !bounds.overlaps(rootBounds)
        // `InvisibleToUser` is deprecated in this Compose version in favor of
        // `HideFromAccessibility`, which is the same signal under a renamed
        // key — checking only the latter avoids a deprecation warning
        // without losing coverage.
        val explicitlyHidden = config.contains(SemanticsProperties.HideFromAccessibility)
        val testTag = config.getOrNull(SemanticsProperties.TestTag)

        return RawSemanticsNode(
            id = node.id,
            testTag = testTag,
            textRuns = config.getOrNull(SemanticsProperties.Text)?.map { it.text } ?: emptyList(),
            contentDescription = config.getOrNull(SemanticsProperties.ContentDescription) ?: emptyList(),
            editableText = config.getOrNull(SemanticsProperties.EditableText)?.text,
            role = config.getOrNull(SemanticsProperties.Role)?.toRawRole(),
            isDialog = config.contains(SemanticsProperties.IsDialog),
            paneTitle = config.getOrNull(SemanticsProperties.PaneTitle),
            liveRegion = config.getOrNull(SemanticsProperties.LiveRegion)?.toRawLiveRegion(),
            collectionInfo = config.getOrNull(SemanticsProperties.CollectionInfo)?.toRawCollectionInfo(),
            isListItem = config.contains(SemanticsProperties.CollectionItemInfo),
            toggleableState = config.getOrNull(SemanticsProperties.ToggleableState)?.toRawToggleableState(),
            focused = config.getOrNull(SemanticsProperties.Focused) ?: false,
            enabled = !config.contains(SemanticsProperties.Disabled),
            selected = config.getOrNull(SemanticsProperties.Selected),
            hasOnClick = config.contains(SemanticsActions.OnClick),
            hasProgressIndicator = config.contains(SemanticsProperties.ProgressBarRangeInfo),
            isSensitive = isSensitive(testTag, config),
            isVisible = !offscreen && !explicitlyHidden,
            isZeroSize = zeroSize,
            children = node.children.map { toRawNode(it, rootBounds) },
        )
    }

    /**
     * docs/07 §5 rule 6's redaction marker. No such convention existed
     * anywhere in `:core:ui`/`:feature:payments` before this task (searched
     * both — no hit for "sensitive"/"REDACTED"), so it is defined here: a
     * `_sensitive` testTag suffix, matching this codebase's existing
     * suffix-convention style for role assignment (`_cta`, `_balance`,
     * `_snackbar`, ...) — the most consistent choice per this task's own
     * framing, and the one a future screen author would reach for by pattern-
     * matching the rest of docs/03 §4's table. Combined, as a bonus backstop,
     * with the real `SemanticsProperties.IsSensitiveData` marker Compose 1.7+
     * ships natively (for autofill/content-capture purposes) — honoring it
     * costs nothing extra and means a screen that sets
     * `Modifier.semantics { isSensitiveData = true }` through the standard
     * Compose mechanism is *also* redacted, not just testTag-tagged fields.
     * [SemanticSnapshotBuilder] layers a third, value-content pattern-match
     * backstop on top of both (docs/07 §5 rule 6: "plus pattern matching as
     * backstop") — independent of this flag entirely.
     */
    private fun isSensitive(testTag: String?, config: SemanticsConfiguration): Boolean =
        testTag?.endsWith(SENSITIVE_TEST_TAG_SUFFIX) == true ||
            config.getOrNull(SemanticsProperties.IsSensitiveData) == true

    private fun <T> SemanticsConfiguration.getOrNull(key: SemanticsPropertyKey<T>): T? =
        if (contains(key)) get(key) else null

    // `Role`/`LiveRegionMode` are Kotlin inline value classes (an `Int` under
    // the hood, matched by identity against named constants), not sealed
    // types or enums — the Kotlin compiler cannot prove a `when` over one is
    // exhaustive, so an `else` is mandatory here even though every value
    // Compose 1.9 actually defines is covered above. Mapping the
    // unreachable-in-practice `else` to `null` — "no role we track" — rather
    // than guessing one of the named constants is the honest choice: it
    // falls through [RoleMapper]'s `node.role == RawRole.BUTTON`/`.TAB`
    // checks the same way "no role at all" already does, instead of
    // silently mislabeling a future Compose role value as `BUTTON`.
    private fun Role.toRawRole(): RawRole? = when (this) {
        Role.Button -> RawRole.BUTTON
        Role.Checkbox -> RawRole.CHECKBOX
        Role.Switch -> RawRole.SWITCH
        Role.RadioButton -> RawRole.RADIO_BUTTON
        Role.Tab -> RawRole.TAB
        Role.Image -> RawRole.IMAGE
        Role.DropdownList -> RawRole.DROPDOWN_LIST
        Role.ValuePicker -> RawRole.VALUE_PICKER
        Role.Carousel -> RawRole.CAROUSEL
        else -> null
    }

    private fun LiveRegionMode.toRawLiveRegion(): RawLiveRegion? = when (this) {
        LiveRegionMode.Polite -> RawLiveRegion.POLITE
        LiveRegionMode.Assertive -> RawLiveRegion.ASSERTIVE
        else -> null
    }

    private fun ToggleableState.toRawToggleableState(): RawToggleableState = when (this) {
        ToggleableState.On -> RawToggleableState.ON
        ToggleableState.Off -> RawToggleableState.OFF
        ToggleableState.Indeterminate -> RawToggleableState.INDETERMINATE
    }

    /**
     * `CollectionInfo(rowCount, columnCount)` is a grid concept; a
     * one-dimensional `LazyColumn`/`LazyRow` sets one of the two to `1`.
     * Judgment call: folding to `max(rowCount, columnCount)` gives the total
     * item count for the common one-dimensional case docs/07 §4's `list`
     * role targets, at the cost of under-reporting a genuinely 2D grid's
     * total (e.g. a 5x5 grid would report 5, not 25) — accepted because
     * VyaparPay has no 2D-grid screen in scope and the IR's `list` role only
     * ever needs "how many total" as a single number.
     */
    private fun CollectionInfo.toRawCollectionInfo(): RawCollectionInfo =
        RawCollectionInfo(totalCount = maxOf(rowCount, columnCount))

    private companion object {
        private const val DEBOUNCE_MILLIS = 300L
        private const val SENSITIVE_TEST_TAG_SUFFIX = "_sensitive"
    }
}
