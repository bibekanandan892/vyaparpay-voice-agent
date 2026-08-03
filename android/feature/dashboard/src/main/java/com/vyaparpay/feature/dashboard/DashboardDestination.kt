package com.vyaparpay.feature.dashboard

/**
 * The merchant home screen (docs/03 §4): wallet balance, today's collections,
 * quick actions, alert banners.
 *
 * [FLOW] is the nav-graph group name that reaches the agent as
 * `screen_context.flow`; the route-to-flow table lives in docs/03 §3.8 and each
 * feature owns its own row of it.
 */
public object DashboardDestination {
    public const val ROUTE: String = "DashboardScreen"
    public const val FLOW: String = "home"
    public const val ROOT_TEST_TAG: String = "dashboard_root"

    /** Operational screen — captured, and the SupportButton is shown here. */
    public const val IS_CAPTURED: Boolean = true
}
