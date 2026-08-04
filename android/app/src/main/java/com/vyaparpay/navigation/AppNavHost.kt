package com.vyaparpay.navigation

import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.navigation.NavHostController
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import com.vyaparpay.feature.dashboard.DashboardRoute
import com.vyaparpay.feature.payments.OrderTrackingRoute
import com.vyaparpay.feature.payments.OrdersRoute
import com.vyaparpay.feature.payments.PaymentRoute
import com.vyaparpay.feature.payments.SettlementsRoute
import com.vyaparpay.feature.support.SupportRoute

/**
 * The root navigation graph.
 *
 * Each destination is a thin call into a feature module's entry-point
 * composable, so `:app` never learns anything about a feature's internals — the
 * only shared vocabulary is the route string and the `flow` name that reaches
 * the agent.
 */
@Composable
public fun AppNavHost(
    modifier: Modifier = Modifier,
    navController: NavHostController = rememberNavController(),
) {
    NavHost(
        navController = navController,
        startDestination = AppRoute.START.route,
        modifier = modifier,
    ) {
        composable(AppRoute.DASHBOARD.route) { DashboardRoute() }
        composable(AppRoute.PAYMENT.route) { PaymentRoute() }
        composable(AppRoute.SETTLEMENTS.route) { SettlementsRoute() }
        composable(AppRoute.ORDERS.route) {
            OrdersRoute(
                onOrderSelected = { navController.navigate(AppRoute.ORDER_TRACKING.route) },
            )
        }
        composable(AppRoute.ORDER_TRACKING.route) {
            OrderTrackingRoute(
                onBack = { navController.popBackStack() },
            )
        }
        composable(AppRoute.SUPPORT.route) { SupportRoute() }
    }
}
