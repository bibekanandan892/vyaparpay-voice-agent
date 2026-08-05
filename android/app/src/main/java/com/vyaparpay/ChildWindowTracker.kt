package com.vyaparpay

import android.os.Build
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.View
import android.view.ViewTreeObserver
import android.view.inspector.WindowInspector
import androidx.compose.runtime.snapshots.ObserverHandle
import androidx.compose.runtime.snapshots.Snapshot
import androidx.compose.ui.node.RootForTest
import java.util.Collections
import java.util.WeakHashMap
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Discovers the *other* Compose windows this process owns — above all the
 * `AlertDialog` docs/01-product-and-use-case.md §7 step 2 builds the whole
 * product on — and reports their [RootForTest] roots as they attach and
 * detach.
 *
 * **The bug this exists to fix (Phase-4 T8f).** A Compose `AlertDialog` is not
 * part of the activity's view hierarchy: `androidx.compose.ui.window.Dialog`
 * builds a real `android.app.Dialog`, with its own `PhoneWindow`, its own
 * decorView, and its own `AndroidComposeView`, and hands it to
 * `WindowManager.addView`. [MainActivity.attachComposeRoot]'s
 * `window.decorView` walk therefore cannot reach it — not at `onCreate`, not
 * ever, because the dialog window does not exist yet at `onCreate` and never
 * becomes a descendant of the main window afterwards. Before this class, the
 * "Daily Limit Exceeded" decline dialog — the single most important screen
 * state in the product, and the trigger for the canonical support call — was
 * absent from every `screen_context/v1` snapshot on a real device, and
 * `SemanticSnapshotBuilder.reconcileDialogBeforeSnackbar` was dead code
 * outside the test suite.
 *
 * ## The discovery mechanism, and the four candidates it beat
 *
 * There is no callback anywhere in the Android framework for "a view was added
 * to this process's `WindowManager`". Discovery therefore has to be a *query*,
 * and the whole design question is which query is legitimate. Four were
 * evaluated against a real constraint each:
 *
 * 1. **Wrapping `Window.Callback` on the activity.** Rejected on the facts:
 *    `Window.Callback` is per-window. The activity's callback never learns
 *    that a *different* window attached; the closest it gets is
 *    `onWindowFocusChanged(false)`, which says "something took focus" without
 *    handing over a reference to it. Useful as a *trigger* (and used as one
 *    below) but useless as a *discovery* mechanism.
 *
 * 2. **`ViewGroup.OnHierarchyChangeListener` / `ViewTreeObserver` on the
 *    decorView.** Rejected for the same structural reason the bug exists at
 *    all: the dialog's decorView is never a child of the activity's decorView,
 *    so no hierarchy or tree observer rooted in the main window ever sees it.
 *
 * 3. **Reflection into `WindowManagerGlobal.mViews`.** This is what Espresso's
 *    `RootsOracle` and Square's Curtains both do, so it demonstrably works —
 *    but it is a private field on a hidden class, reached from an app whose
 *    `targetSdk` is 36. Non-SDK interface restrictions (API 28+) gate exactly
 *    this kind of access on a list Google revises every release, and a
 *    fintech app that silently loses screen context mid-call after an OS
 *    update is precisely the failure docs/08 §7 exists to prevent. Rejected in
 *    favour of (4), which is the same data through the front door.
 *
 * 4. **`WindowInspector.getGlobalWindowViews()` — chosen.** Public SDK since
 *    API 29 (verified against `platforms/android-36/android.jar`, not
 *    inferred): `public static List<View> getGlobalWindowViews()`, returning
 *    the root view of every window this process has attached. It is the
 *    supported spelling of exactly what candidate 3 reaches for by reflection.
 *
 * Compose's own `DialogWindowProvider` was the fifth candidate and is worth
 * naming because it looks like the idiomatic answer: a Compose dialog's
 * `DialogLayout` really does implement it, so `view.parent as?
 * DialogWindowProvider` yields the dialog's `Window`. But it answers "given
 * this view, which window is it in?" — it is a *lookup*, not an *enumeration*,
 * and reaching a view inside the dialog's subcomposition requires cooperation
 * from the dialog's own content, which lives in `:feature:*` code this task
 * does not own and which no future screen author should have to remember.
 *
 * ## Why this has no global-singleton hazard
 *
 * [UiTreeCollector][com.vyaparpay.core.screencontext.UiTreeCollector]'s kdoc
 * is emphatic that nothing may wire itself to
 * `ViewRootForTest.Companion.onViewCreatedCallback`: it is a process-wide
 * singleton `var` that `createAndroidComposeRule` already claims, and a second
 * writer clobbers it and breaks compose-ui-test mid-test. This class touches
 * none of that, and the distinction is the reason it is safe:
 *
 * * [WindowInspector.getGlobalWindowViews] is a **read** of process-global
 *   state, not a claim on it. There is no slot to own, so there is no second
 *   writer to lose to, no matter how many other libraries in the process also
 *   call it.
 * * `Snapshot.registerApplyObserver` (the trigger, below) **appends** to an
 *   observer list and returns an [ObserverHandle] to remove exactly that
 *   entry. `UiTreeCollector.start()` already registers one of its own; two
 *   registrations coexist by design — this is the additive-listener shape, not
 *   the single-slot shape.
 *
 * ## The trigger, and why two of them
 *
 * A query needs an occasion to run. Two cheap signals drive it, both merely
 * saying "look again" — neither is trusted to be complete on its own:
 *
 * * **`Snapshot.registerApplyObserver`.** A Compose dialog only ever appears
 *   because Compose state changed, so every dialog is preceded by a snapshot
 *   apply. The apply that *causes* the dialog fires a frame before the window
 *   actually attaches, so that first look finds nothing — but composing,
 *   measuring and focusing the new window is itself a burst of further
 *   applies, and the scan lands on one of those. This is also why the scan
 *   must stay cheap and idempotent: it runs often and usually finds nothing.
 * * **`ViewTreeObserver.OnWindowFocusChangeListener` on the host window.** The
 *   activity losing focus is the framework telling us, after the fact, that
 *   another window is now on top — the one trigger that does not depend on
 *   Compose continuing to commit state, and the one that still fires for a
 *   window put up by something other than a recomposition.
 *
 * Both funnel into a single main-thread scan coalesced through [scanPending],
 * so a burst of applies costs one scan, and the snapshot observer stays safe
 * to call from whatever thread applied the snapshot.
 *
 * Latency is deliberately not chased to zero. Discovery lands within a frame
 * or two, and [UiTreeCollector.attachRoot][com.vyaparpay.core.screencontext.UiTreeCollector.attachRoot]
 * then bypasses the 300 ms debounce for an immediate capture — so a newly
 * attached window still reaches the agent well inside the budget docs/07 §2.1
 * already accepts for everything else.
 *
 * ## Symmetry, and why detach does NOT reuse the attach trigger
 *
 * Detach runs off `View.OnAttachStateChangeListener`, registered on each
 * window root at the moment it is discovered — the framework's own exact
 * statement that this window is gone, delivered by `WindowManagerGlobal` as
 * it tears the window down.
 *
 * The first version of this class derived detach from the same scan as
 * attach — a window absent from [WindowInspector.getGlobalWindowViews] is
 * gone, one source of truth, both directions. It is tidier, and it does not
 * work, which `MainActivityDialogWindowCaptureTest` demonstrated before this
 * comment was written: dismissal is the *last* thing a dialog does. The state
 * change that dismisses it fires a snapshot apply, the scan that apply
 * schedules runs a beat before the window is actually removed, and then
 * nothing further happens — no more composition, no more applies, no trigger,
 * so the stale root sat in `attachedRoots` forever. Attach gets away with the
 * scan precisely because a *new* window keeps generating work (composing,
 * measuring, taking focus) for the scan to ride in on; a *closing* window
 * generates none. The asymmetry in the mechanism is a faithful reflection of
 * an asymmetry in the thing being observed.
 *
 * The scan still removes any window that has vanished without its listener
 * firing, so the two paths converge on the same answer rather than competing;
 * `forget` is idempotent, so whichever arrives first wins and the second is a
 * no-op. [stop] detaches everything still tracked, so the collector's
 * `attachedRoots` never outlives the activity that populated it (it is an
 * app-lifetime `@Singleton`, so this is a real leak, not a theoretical one).
 *
 * ## Degradation
 *
 * docs/08 §7: degrade context, never conversation. Two ways this gives up, and
 * both leave the app and the call untouched, with main-window-only capture
 * still running:
 *
 * * **API 24-28.** [WindowInspector] is API 29+, and `minSdk` is 24. Those
 *   devices get no dialog capture. That is a real, named gap, chosen over
 *   plugging it with candidate 3's reflection — a hidden-API path that cannot
 *   be exercised by any test in this repo and whose failure mode on an
 *   unknown OEM build is unknowable. It is written down here rather than
 *   quietly worked around.
 * * **Anything thrown.** Every framework call that can realistically fail is
 *   wrapped; the first [Throwable] logs once and latches [degraded], after
 *   which this class is inert for the rest of the process. `Throwable`, not
 *   `Exception`, is deliberate: a future SDK could turn hidden-API-ish access
 *   into an `Error`, and there is no failure here worth taking a live support
 *   call down for.
 *
 *   Two calls are deliberately NOT wrapped, and the exclusion is named rather
 *   than left for a reader to discover as a discrepancy (review fix, LOW —
 *   this paragraph used to claim *every* call was wrapped, which was false of
 *   exactly these two): [Handler.post] and
 *   [Handler.removeCallbacksAndMessages] on a live main-looper handler are
 *   queue operations with no failure mode short of OOM, and wrapping an
 *   infallible call would imply a risk that isn't there while adding a
 *   swallow point that could hide a genuine future change in their contract.
 *
 * @param hostView the activity's `window.decorView` — both the window to
 *   exclude (its root is [MainActivity]'s own responsibility) and the
 *   `ViewTreeObserver` the focus trigger hangs off.
 * @param onRootAttached invoked on the main thread, once per newly discovered
 *   window; wired to `UiTreeCollector::attachRoot`.
 * @param onRootDetached invoked on the main thread, once per window that has
 *   gone away; wired to `UiTreeCollector::detachRoot`.
 */
internal class ChildWindowTracker(
    private val hostView: View,
    private val onRootAttached: (RootForTest) -> Unit,
    private val onRootDetached: (RootForTest) -> Unit,
) {

    private val handler = Handler(Looper.getMainLooper())

    /**
     * Discovered windows, keyed by the window root view [WindowInspector]
     * hands back. A `LinkedHashMap` because attach ORDER is load-bearing
     * downstream: `UiTreeCollector.attachedRoots` is walked in order and
     * `SemanticSnapshotBuilder`'s dialog-before-snackbar reconciliation reads
     * that order as "which window came up last".
     */
    private val trackedWindows = LinkedHashMap<View, TrackedWindow>()


    /** Cross-thread: [Snapshot] apply observers fire on whichever thread applied. */
    private val scanPending = AtomicBoolean(false)

    private var applyObserver: ObserverHandle? = null
    private var focusListener: ViewTreeObserver.OnWindowFocusChangeListener? = null

    // @Volatile on the three flags below (review fix, LOW). All three are
    // written on the main thread but READ from [scheduleScan], which a snapshot
    // apply can invoke on whichever thread did the applying. A reviewer traced
    // the actual guarantee through `androidx.compose.runtime`'s source and
    // found it already holds without `@Volatile`: `registerApplyObserver` adds
    // the observer under a lock that the apply-notification path also takes
    // before invoking observers, which establishes a happens-before edge
    // carrying the `started = true` write along with it. That is a correct
    // argument and it is also an argument about someone else's private
    // implementation detail — it would evaporate silently if Compose ever
    // restructured that locking, and nothing here would fail loudly enough to
    // notice. `@Volatile` costs a read barrier on a path that already posts to
    // a Handler, and makes the safety self-evident from this file alone.
    @Volatile private var started = false

    /** Terminal once [stop] has run — [start] must not resurrect a torn-down tracker. */
    @Volatile private var terminated = false

    /** Latched by [degrade]; see the class kdoc's degradation section. */
    @Volatile private var degraded = false

    /**
     * Begins watching for sibling windows. Call once, on the main thread,
     * after the host window's own root has been attached — running an
     * immediate first scan is part of the contract, so anything already up
     * (a dialog restored across a configuration change) is picked up without
     * waiting for a trigger.
     */
    fun start() {
        if (started || terminated) return
        started = true
        liveHostViews += hostView

        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) {
            degraded = true
            Log.i(
                TAG,
                "Dialog-window capture unavailable below API 29 " +
                    "(WindowInspector.getGlobalWindowViews); continuing with main-window-only capture.",
            )
            return
        }

        try {
            applyObserver = Snapshot.registerApplyObserver { _, _ -> scheduleScan() }
            focusListener = ViewTreeObserver.OnWindowFocusChangeListener { scheduleScan() }
                .also { hostView.viewTreeObserver.addOnWindowFocusChangeListener(it) }
        } catch (throwable: Throwable) {
            degrade("could not register window-discovery triggers", throwable)
            return
        }

        scan()
    }

    /**
     * Stops watching and reports every still-tracked window as detached, so
     * the collector this feeds is left exactly as it was before [start].
     */
    fun stop() {
        if (terminated) return
        terminated = true
        started = false
        liveHostViews -= hostView

        try {
            applyObserver?.dispose()
            focusListener?.let { hostView.viewTreeObserver.removeOnWindowFocusChangeListener(it) }
        } catch (throwable: Throwable) {
            // Teardown is best-effort by definition: a ViewTreeObserver whose
            // window is already gone throws, and there is nothing left to fix.
            Log.w(TAG, "Ignoring failure while unregistering window-discovery triggers", throwable)
        }
        applyObserver = null
        focusListener = null
        handler.removeCallbacksAndMessages(null)
        scanPending.set(false)

        trackedWindows.keys.toList().forEach(::forget)
    }

    /**
     * Coalesces every trigger onto one main-thread scan. Safe to call from any
     * thread — which it must be, since a snapshot apply is not required to
     * happen on the UI thread.
     */
    private fun scheduleScan() {
        if (!started || degraded) return
        if (!scanPending.compareAndSet(false, true)) return
        handler.post {
            scanPending.set(false)
            scan()
        }
    }

    /**
     * Discovery, in one idempotent pass: every window this process currently
     * has attached that is not the host and not already tracked becomes a new
     * root, and anything tracked that has vanished without its
     * [DetachListener] firing is swept up behind it.
     *
     * A window with no [RootForTest] in it is skipped rather than remembered —
     * it is either not a Compose window at all (an in-process toast, an IME
     * decor) or one whose composition has not been created yet, and a later
     * scan will find it once it has. Skipping keeps [trackedWindows] meaning
     * exactly one thing: windows currently reported to the collector.
     */
    private fun scan() {
        if (degraded || !started) return

        val windowViews = try {
            currentWindowViews()
        } catch (throwable: Throwable) {
            degrade("could not enumerate this process's windows", throwable)
            return
        }

        for (windowView in windowViews) {
            if (windowView === hostView || trackedWindows.containsKey(windowView)) continue
            if (windowView in liveHostViews) continue
            val root = try {
                findRootForTest(windowView)
            } catch (throwable: Throwable) {
                degrade("could not walk a discovered window for its Compose root", throwable)
                return
            } ?: continue

            try {
                val detachListener = DetachListener()
                windowView.addOnAttachStateChangeListener(detachListener)
                trackedWindows[windowView] = TrackedWindow(root, detachListener)
                onRootAttached(root)
            } catch (throwable: Throwable) {
                degrade("could not begin tracking a discovered window", throwable)
                return
            }
        }

        // Backstop only — `DetachListener` is the path that normally fires.
        // See the class kdoc's symmetry section for why both exist.
        trackedWindows.keys
            .filterNot { tracked -> windowViews.any { it === tracked } }
            .forEach(::forget)
    }

    /**
     * Stops tracking [windowView] and reports its root detached, exactly once
     * however many paths reach this. Idempotent by construction: the
     * `remove` either yields the entry or the work has already been done.
     */
    private fun forget(windowView: View) {
        val tracked = trackedWindows.remove(windowView) ?: return
        try {
            windowView.removeOnAttachStateChangeListener(tracked.detachListener)
        } catch (throwable: Throwable) {
            Log.w(TAG, "Ignoring failure while unregistering a window detach listener", throwable)
        }
        reportDetached(tracked.root)
    }

    private inner class DetachListener : View.OnAttachStateChangeListener {
        override fun onViewAttachedToWindow(view: View): Unit = Unit
        override fun onViewDetachedFromWindow(view: View): Unit = forget(view)
    }

    private class TrackedWindow(
        val root: RootForTest,
        val detachListener: View.OnAttachStateChangeListener,
    )

    /**
     * The `SDK_INT` guard is repeated here rather than relying on [start]'s
     * earlier one: lint's `NewApi` check only recognises a version guard in
     * the same function as the call, and a suppression would hide a real
     * mistake later. The `emptyList()` branch is unreachable in practice —
     * [start] has already latched [degraded] below API 29 — so it is written
     * as the honest "no windows visible to us" answer rather than a throw.
     */
    private fun currentWindowViews(): List<View> =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            WindowInspector.getGlobalWindowViews()
        } else {
            emptyList()
        }

    private fun reportDetached(root: RootForTest) {
        try {
            onRootDetached(root)
        } catch (throwable: Throwable) {
            degrade("the detach callback threw", throwable)
        }
    }

    private fun degrade(what: String, throwable: Throwable) {
        degraded = true
        Log.w(
            TAG,
            "Dialog-window capture disabled for this process: $what. " +
                "Continuing with main-window-only capture.",
            throwable,
        )
    }

    private companion object {
        const val TAG = "ChildWindowTracker"

        /**
         * Every live tracker's own host window, so no tracker ever adopts
         * another Activity's main window (Phase-4 T8e review, HIGH).
         *
         * **The bug.** [WindowInspector] enumerates every window in the
         * PROCESS, and [scan] excluded only this tracker's own [hostView]. That
         * was unreachable while two `MainActivity` instances could not coexist
         * — `UiTreeCollector.start()` threw on the second — but reference
         * counting `start()` made the overlap survivable and this reachable:
         * each Activity's tracker adopted the *other's* decorView. Measured
         * with two live activities: 4 roots for 2 windows. `attachRoot` bypasses
         * the debounce, and `SemanticSnapshotBuilder` flat-maps every root with
         * no dedup, so the next snapshot carried both screens.
         *
         * **Why a registry and not the window's own context.** The obvious fix
         * — resolve each window's owning Activity through its context chain —
         * does not work, and the attempt is worth recording so nobody retries
         * it. A decorView's context chain is `DecorContext -> ContextImpl`:
         * `android.view.DecorContext` deliberately wraps the *application*
         * context rather than the Activity, precisely so a decorView cannot
         * leak one. Measured on both activities in
         * `MainActivityOverlappingWindowsTest`'s probe — the chain contains no
         * `Activity` at all, so ownership is simply not recoverable that way.
         * "Scan only while resumed" was the other candidate and is worse: the
         * outgoing Activity is already paused while a dialog it owns is still
         * legitimately on screen.
         *
         * Main-thread confined — [start] and [stop] run from Activity
         * `onCreate`/`onDestroy`, and [scan] is posted to the main looper.
         * Weakly held so a tracker that never gets [stop]ped cannot pin a
         * decorView.
         */
        private val liveHostViews: MutableSet<View> =
            Collections.newSetFromMap(WeakHashMap<View, Boolean>())
    }
}
