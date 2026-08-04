package com.vyaparpay.feature.payments

/**
 * [SettlementsViewModel]'s single state object (docs/03-android-architecture.md
 * §2: one `StateFlow<UiState>` per screen, unidirectional data flow).
 *
 * Each field maps onto this task's SettlementsScreen testTag -> role table:
 * [netSettledValue] -> `settlements_net_balance` (`balance_display`),
 * [feesValue] -> `settlements_fees_balance` (`balance_display`),
 * [latestBatchStatus] -> `latest_batch_status` (`status_badge`, header,
 * OUTSIDE the list), [shortfallMessage] -> `shortfall_alert` (`alert_banner`,
 * shown only when non-null), [rows]/[totalCount] -> `settlement_batches_list`
 * / `settlement_batch_row_$index` (`list` / `list_item`).
 */
public data class SettlementsUiState(
    public val isLoading: Boolean = true,
    public val netSettledValue: String = "",
    public val feesValue: String = "",
    public val latestBatchStatus: String = "",
    public val shortfallMessage: String? = null,
    public val rows: List<SettlementRowUiModel> = emptyList(),
    public val totalCount: Int = 0,
)

/**
 * One `settlement_batch_row_$index` — exactly two text runs, per the hard
 * `SemanticSnapshotBuilder.fillListVisibleCounts` / `DropLadder.rung1CapListItems`
 * contiguity constraint this task's brief spells out: [label] and [value] are
 * the row's only text, and [value] deliberately folds the batch's status IN
 * as plain text rather than exposing it as a separate `_status`-suffixed
 * child node — a per-row status badge would insert a second role-resolvable
 * node between consecutive `list_item`s, breaking the
 * `[list, list_item, list_item, ...]` contiguity both of those functions walk
 * to compute `visible_count` / decide what the drop ladder caps.
 */
public data class SettlementRowUiModel(
    public val settlementId: String,
    public val label: String,
    public val value: String,
)
