package com.vyaparpay.feature.dashboard

/**
 * [DashboardViewModel]'s single state object (docs/03-android-architecture.md
 * §2: one `StateFlow<UiState>` per screen, unidirectional data flow).
 *
 * Fields map onto the docs/01-product-and-use-case.md §5 `DashboardScreen`
 * roles (docs/07-ui-semantic-context.md §4 role vocabulary in parens):
 * [walletBalancePaise] -> `balance_display` (`wallet_balance`),
 * [collectionsTodayPaise] -> `balance_display` (`collections_today_balance`),
 * [settlementEtaLabel] -> `status_badge` (`settlement_eta_status`),
 * [alertMessage] -> `alert_banner` (`kyc_alert`). [isLoading] backs the
 * on-screen progress indicator, which is what `SemanticSnapshotBuilder`'s
 * "any visible progress indicator survives pruning" check (docs/07 §5 rule 4)
 * reflects into the IR's top-level `loading` flag.
 */
public data class DashboardUiState(
    public val isLoading: Boolean = true,
    public val walletBalancePaise: Long = 0L,
    public val collectionsTodayPaise: Long = 0L,
    public val settlementEtaLabel: String = "",
    public val alertMessage: String? = null,
)
