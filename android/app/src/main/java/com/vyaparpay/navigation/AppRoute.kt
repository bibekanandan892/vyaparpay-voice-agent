package com.vyaparpay.navigation

import com.vyaparpay.feature.dashboard.DashboardDestination
import com.vyaparpay.feature.payments.OrdersDestination
import com.vyaparpay.feature.payments.PaymentDestination
import com.vyaparpay.feature.payments.SettlementsDestination
import com.vyaparpay.feature.support.SupportDestination

/**
 * The app's five destinations, assembled from the constants each feature owns.
 *
 * This enum is the assembly point, not the source of truth: a feature declares
 * its own route, flow and capture policy, and `:app` only decides which of them
 * are reachable. That keeps the route-to-flow table of docs/03 §3.8 in one place
 * per feature instead of duplicated into a central registry that drifts.
 *
 * `NavigationTracker` binds to the resulting `NavController` and turns
 * destination changes into `flow` updates, `nav` timeline events, and an
 * immediate capture that bypasses the 300 ms debounce.
 */
public enum class AppRoute(
    public val route: String,
    public val flow: String,
    public val isCaptured: Boolean,
) {
    DASHBOARD(
        route = DashboardDestination.ROUTE,
        flow = DashboardDestination.FLOW,
        isCaptured = DashboardDestination.IS_CAPTURED,
    ),
    PAYMENT(
        route = PaymentDestination.ROUTE,
        flow = PaymentDestination.FLOW,
        isCaptured = PaymentDestination.IS_CAPTURED,
    ),
    SETTLEMENTS(
        route = SettlementsDestination.ROUTE,
        flow = SettlementsDestination.FLOW,
        isCaptured = SettlementsDestination.IS_CAPTURED,
    ),
    ORDERS(
        route = OrdersDestination.ROUTE,
        flow = OrdersDestination.FLOW,
        isCaptured = OrdersDestination.IS_CAPTURED,
    ),
    SUPPORT(
        route = SupportDestination.ROUTE,
        flow = SupportDestination.FLOW,
        isCaptured = SupportDestination.IS_CAPTURED,
    ),
    ;

    public companion object {
        /** The merchant lands on their own numbers, not on a menu. */
        public val START: AppRoute = DASHBOARD

        /** What the SupportButton is shown on, and what a snapshot may describe. */
        public val captured: List<AppRoute> get() = entries.filter { it.isCaptured }

        public fun fromRoute(route: String?): AppRoute? = entries.find { it.route == route }
    }
}
