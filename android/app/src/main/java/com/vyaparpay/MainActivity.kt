package com.vyaparpay

import android.os.Bundle
import android.view.View
import android.view.ViewGroup
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.Surface
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.Modifier
import androidx.compose.ui.node.RootForTest
import androidx.navigation.compose.rememberNavController
import com.vyaparpay.core.screencontext.NavigationTracker
import com.vyaparpay.core.screencontext.UiTreeCollector
import com.vyaparpay.core.ui.theme.VyaparTheme
import com.vyaparpay.navigation.AppNavHost
import dagger.hilt.android.AndroidEntryPoint
import javax.inject.Inject

/**
 * The app's single activity. Everything below it is Compose.
 *
 * One activity is what makes the context pipeline tractable: `UiTreeCollector`
 * tracks window attach/detach against one host rather than reconciling a stack
 * of activities, and `NavigationTracker` binds once to one `NavController`.
 *
 * **Phase-4 T8a — the production wiring judgment call.** Every piece below
 * ([UiTreeCollector], [NavigationTracker], the `EventTracker`/`CoroutineScope`
 * Hilt bindings) already existed, unit-tested, but nothing in the running app
 * called any of it. This class is where that happens:
 *
 * 1. **Field injection, not constructor injection.** `ComponentActivity`'s
 *    constructor is framework-owned; `@AndroidEntryPoint` classes inject via
 *    `lateinit var` fields populated in `Hilt_MainActivity`'s generated
 *    `onCreate` (which runs before this class's own `super.onCreate(...)`
 *    call returns) — the only injection shape available here, and the same
 *    one every other `@AndroidEntryPoint` Activity in the Android ecosystem
 *    uses. `internal`, not `private`: [MainActivityScreenContextTest] reads
 *    both fields directly to assert on the real, injected instances rather
 *    than re-deriving equivalent ones.
 *
 * 2. **`uiTreeCollector.start()` before `setContent { }`, not after.**
 *    [UiTreeCollector.start] only needs to run once, on the UI thread, before
 *    the first capture matters — calling it before Compose's first commit
 *    guarantees the debounced-capture observer is already registered by the
 *    time there is anything to observe, rather than racing the first frame.
 *
 * 3. **The `NavHostController` is hoisted here, not left inside
 *    `AppNavHost`'s own default.** [NavigationTracker.bind] needs a live
 *    reference to bind against; `AppNavHost`'s `navController` parameter
 *    already defaulted to `rememberNavController()` for exactly this kind of
 *    override (docs/03's own `state`-down/`events`-up convention applied one
 *    level up: `:app` owns navigation identity, `AppNavHost` just renders
 *    against whichever controller it's given). `rememberNavController()` is
 *    called directly inside this `setContent { }` block — composed exactly
 *    once for the activity's lifetime — and passed down explicitly.
 *
 * 4. **`navigationTracker.bind(navController)` runs inside a
 *    `LaunchedEffect(Unit)`, not directly in the composable body.**
 *    [NavigationTracker.bind] is not idempotent (it unconditionally calls
 *    `NavController.addOnDestinationChangedListener`, which stacks a new
 *    listener — and a duplicate `nav` timeline event per destination change —
 *    on every call); calling it directly in a `@Composable` body would
 *    re-bind on every recomposition. `LaunchedEffect(Unit)` keyed to the
 *    composition's own lifetime is the standard Compose idiom for exactly
 *    this "run once, not on every recomposition" shape, even though `bind`
 *    itself is not a suspend function.
 *
 * 5. **The `RootForTest` decorView walk is posted, not run inline in
 *    `onCreate`.** `setContent { }` schedules composition; it does not
 *    render synchronously before `onCreate` returns, so a `RootForTest`
 *    walk of `window.decorView` run immediately after `setContent { }` would
 *    find nothing yet — verified empirically against a Robolectric-rendered
 *    `MainActivity` in [MainActivityScreenContextTest], not assumed. Posting
 *    via `window.decorView.post { }` defers the walk until after the view
 *    hierarchy Compose builds has actually attached, matching the exact
 *    `findRootForTest` technique `UiTreeCollectorPaymentScreenCanaryTest`/
 *    `SettlementsScreenContextCanaryTest` (`:feature:payments`) already
 *    establish against a test-only Compose tree — see [UiTreeCollector]'s own
 *    kdoc ("Multi-window tracking") for why that technique, not
 *    `ViewRootForTest.Companion.onViewCreatedCallback`, is the correct one.
 *
 * 6. **Lifecycle: `start`/`attachRoot` in `onCreate`, `stop` in `onDestroy`.**
 *    This is a single-activity app, so `onDestroy` only fires on a real
 *    process-level teardown or a configuration change recreating the
 *    activity — either way, a fresh `MainActivity` instance (and, via Hilt,
 *    a freshly injected `uiTreeCollector`/`navigationTracker` pair scoped to
 *    the same app-lifetime singletons) runs `onCreate` again, so there is no
 *    observer leaked across recreation. `UiTreeCollector.start`/`stop` are
 *    reference-counted (see that class's kdoc) precisely because the incoming
 *    `onCreate` runs *before* the outgoing `onDestroy` on a configuration
 *    change — the observer therefore survives the handover rather than being
 *    torn down by the Activity that is leaving.
 *
 *    **`onDestroy` DOES detach the main window (corrected in Phase-4 T8e).**
 *    This previously did not, on the reasoning that `detachRoot` is for a
 *    window closing while the activity is still alive. That reasoning depended
 *    on the collector dying with the Activity, which stopped being true when
 *    `UiTreeCollector` was correctly scoped `@Singleton`: an unbalanced
 *    `attachRoot` then pins every destroyed Activity's `AndroidComposeView` —
 *    and through its Context the Activity and its whole semantics tree — in an
 *    app-lifetime object, one per rotation, dark-mode toggle, font-size or
 *    locale change (this app declares no `configChanges`). Detaching here is
 *    the precise balance for the `attachRoot` in `attachComposeRoot`, and it
 *    is what keeps the overlap case correct: each instance removes exactly its
 *    own window, so the incoming Activity's root is untouched.
 *    `UiTreeCollector.stop` additionally clears any remaining roots when the
 *    last host leaves. Sibling windows are a different matter entirely: see
 *    judgment call 7.
 *
 * 7. **Sibling windows are tracked by [ChildWindowTracker], not by a second
 *    `decorView` walk (Phase-4 T8f).** Judgment call 5's walk is complete for
 *    the window it is given and structurally blind to every other one: a
 *    Compose `AlertDialog` is its own `android.app.Dialog`, with its own
 *    decorView and its own `AndroidComposeView`, attached straight to
 *    `WindowManager` and never a descendant of `window.decorView`. Until this
 *    task, the "Daily Limit Exceeded" dialog docs/01 §7 step 2 builds the
 *    entire product on was therefore missing from every snapshot the running
 *    app produced. [ChildWindowTracker] closes that hole and owns the
 *    `attachRoot`/`detachRoot` PAIR for those windows — a dialog dismisses as
 *    well as appears, and an unbalanced `attachRoot` would leave
 *    `UiTreeCollector.attachedRoots` (an app-lifetime `@Singleton`)
 *    accumulating dead windows and republishing stale dialog components
 *    forever. Its own kdoc carries the discovery-mechanism reasoning; what
 *    belongs here is the division of labour, which is deliberate: the main
 *    window keeps the direct, proven walk below (it is the one window whose
 *    existence and lifetime this activity already knows for certain), and the
 *    tracker is scoped to exactly the windows nothing else can see. Folding
 *    the main window into the tracker too would be tidier on paper and would
 *    trade a mechanism proven by [MainActivityScreenContextTest] for one that
 *    has to re-derive the same answer through a process-wide query — no gain,
 *    real risk.
 */
@AndroidEntryPoint
public class MainActivity : ComponentActivity() {

    @Inject
    internal lateinit var uiTreeCollector: UiTreeCollector

    @Inject
    internal lateinit var navigationTracker: NavigationTracker

    /**
     * Created eagerly in `onCreate` but only armed once the Compose tree
     * exists (judgment call 7). `internal` for the same reason the two
     * injected fields are: [MainActivityDialogWindowCaptureTest] asserts
     * against the real instance this activity built, not an equivalent one.
     */
    internal lateinit var childWindowTracker: ChildWindowTracker
        private set

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        uiTreeCollector.start()
        childWindowTracker = ChildWindowTracker(
            hostView = window.decorView,
            onRootAttached = uiTreeCollector::attachRoot,
            onRootDetached = uiTreeCollector::detachRoot,
        )

        setContent {
            val navController = rememberNavController()
            LaunchedEffect(Unit) {
                navigationTracker.bind(navController)
            }
            VyaparTheme {
                Surface(modifier = Modifier.fillMaxSize()) {
                    AppNavHost(navController = navController)
                }
            }
        }

        window.decorView.post { attachComposeRoot() }
    }

    override fun onDestroy() {
        // Before `uiTreeCollector.stop()`: the tracker's own stop() reports
        // every sibling window it is still tracking as detached, and that has
        // to land on a collector that is still listening.
        childWindowTracker.stop()
        // Balances the attachRoot in `attachComposeRoot` — see judgment call 6.
        findRootForTest(window.decorView)?.let(uiTreeCollector::detachRoot)
        uiTreeCollector.stop()
        super.onDestroy()
    }

    private fun attachComposeRoot() {
        findRootForTest(window.decorView)?.let(uiTreeCollector::attachRoot)
        // Started here, not in `onCreate`, for the same timing reason the walk
        // above is posted: `ChildWindowTracker.start()` runs an immediate first
        // scan, and running it before the host window has attached would make
        // that scan's "skip the host window" rule moot and its first result
        // meaningless.
        childWindowTracker.start()
    }
}

/**
 * The same decorView-walk `UiTreeCollectorPaymentScreenCanaryTest`/
 * `SettlementsScreenContextCanaryTest` (`:feature:payments`) each define as a
 * private test helper — see [UiTreeCollector]'s own kdoc for why this,
 * rather than `ViewRootForTest.Companion.onViewCreatedCallback`, is the
 * correct discovery technique in production too. `internal`, not `private`,
 * so a test in this module could reach it directly if a narrower,
 * Hilt-free companion to [MainActivityScreenContextTest] is ever worth
 * adding again — an earlier version of that test tried exactly that against
 * a bare `ComponentActivity`, but `:app`'s real, restrictive
 * `AndroidManifest.xml` (only `.MainActivity` is declared) makes a bare
 * `ComponentActivity` unresolvable to Robolectric's `ActivityScenario`
 * inside this module, confirmed empirically; [MainActivityScreenContextTest]
 * proves this function's real call site end to end instead, with higher
 * fidelity than that narrower test would have.
 */
internal fun findRootForTest(view: View): RootForTest? {
    if (view is RootForTest) return view
    if (view is ViewGroup) {
        for (i in 0 until view.childCount) {
            findRootForTest(view.getChildAt(i))?.let { return it }
        }
    }
    return null
}
