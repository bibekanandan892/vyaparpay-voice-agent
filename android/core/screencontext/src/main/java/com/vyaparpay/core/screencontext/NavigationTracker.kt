package com.vyaparpay.core.screencontext

import androidx.navigation.NavController
import com.vyaparpay.core.analytics.AppEvent
import com.vyaparpay.core.analytics.EventTracker
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * Tracks the current destination and back stack, maps the route to a `flow`
 * name for the `screen_context/v1` IR, and emits `nav` events into the
 * timeline (docs/03 §3.8).
 *
 * **DI note (updated Phase-4 T8a).** [Inject] and [Singleton] were originally
 * added so this class would be Hilt-ready once `:core:screencontext` applied
 * the Hilt Gradle plugin and `:app`/`MainActivity` called [bind] — both now
 * true. `:core:screencontext`'s `build.gradle.kts` applies `hilt`/`ksp`
 * (see that file's own comment), and `MainActivity.onCreate` binds the real
 * `NavHostController` it hoists out of `AppNavHost` — see `MainActivity`'s
 * own kdoc for the full lifecycle wiring.
 *
 * On each destination change: (a) update [route]/[flow], (b) fire a `nav`
 * event with `from` = previous route into [EventTracker]. The doc's third
 * step — "triggers an immediate context capture (the debounce bypass)" — is
 * `ScreenContextPublisher`'s job and is out of scope here: that class needs
 * `SemanticSnapshotBuilder`'s `ScreenContextIr`, which does not exist yet.
 *
 * @param clock defaults to the wall clock; overridable so tests can pin `ts`.
 *
 * **Second bug found and fixed alongside `ApiErrorReporter`'s identical one
 * while completing this task's Hilt wiring.** `@Inject` sat on this
 * constructor with [clock] defaulted; Dagger/Hilt ignores Kotlin default
 * parameter values, so `:app:assembleDebug`'s Hilt aggregation demanded an
 * unqualified `kotlin.jvm.functions.Function0<Long>` binding nothing
 * provides — the exact same latent defect `ApiErrorReporter.kt`'s own kdoc
 * now documents, present here since this class's `@Inject`/`@Singleton`
 * were first added and equally never caught by a completed
 * `:app:assembleDebug` run before this task. Fixed the same way: [clock]
 * moved off the `@Inject`-annotated constructor, onto a narrower one below.
 */
@Singleton
public class NavigationTracker(
    private val events: EventTracker,
    private val clock: () -> Long,
) {

    /** Delegates to [System.currentTimeMillis] -- see this class's own kdoc for why this, not the primary constructor, carries `@Inject`. */
    @Inject
    public constructor(events: EventTracker) : this(events, System::currentTimeMillis)

    private val _route = MutableStateFlow(UNKNOWN_ROUTE)

    /** The current destination's route string. */
    public val route: StateFlow<String> = _route.asStateFlow()

    private val _flow = MutableStateFlow(UNKNOWN_FLOW)

    /** The nav-graph group this route belongs to — `screen_context.flow`. */
    public val flow: StateFlow<String> = _flow.asStateFlow()

    /** Binds to the root [NavController]; listener fires on main (docs/03 §3.8). */
    public fun bind(controller: NavController) {
        controller.addOnDestinationChangedListener { _, destination, _ ->
            onDestinationChanged(destination.route ?: UNKNOWN_ROUTE)
        }
    }

    /**
     * The testable core of a destination change, decoupled from
     * [NavController] (an Android framework type this module cannot easily
     * fake in a plain-JUnit test).
     */
    internal fun onDestinationChanged(newRoute: String) {
        val previousRoute = _route.value
        _route.value = newRoute
        _flow.value = flowFor(newRoute)
        events.record(AppEvent.Nav(name = newRoute, ts = clock(), from = previousRoute))
    }

    /** The docs/03 §3.8 static route → flow table. Unmapped routes fold to [UNKNOWN_FLOW]. */
    internal fun flowFor(route: String): String = ROUTE_TO_FLOW[route] ?: UNKNOWN_FLOW

    private companion object {
        private const val UNKNOWN_ROUTE = ""
        private const val UNKNOWN_FLOW = ""

        /**
         * docs/03 §3.8's table verbatim. `:core:screencontext` cannot depend
         * on `:app` or any `:feature:*` module (docs/03 §1: `:core:*` may
         * only depend on `:core:*`), so this table is necessarily this
         * module's own copy rather than a read of `AppRoute`/the per-feature
         * `Destination` objects — those already carry the same values, and
         * this table is the doc's own home for the mapping regardless.
         */
        private val ROUTE_TO_FLOW: Map<String, String> = mapOf(
            "PaymentScreen" to "vendor_payment",
            "SettlementsScreen" to "settlements_review",
            "OrdersScreen" to "device_orders",
            "OrderTrackingScreen" to "device_orders",
            "DashboardScreen" to "home",
            // Support surfaces map to "support" but are excluded from capture
            // (docs/07 §2.1) — that exclusion is ScreenContextPublisher's job,
            // out of scope here; this table only owns route -> flow.
            "HelpScreen" to "support",
            "ConversationOverlay" to "support",
        )
    }
}
