package com.vyaparpay.feature.payments

/**
 * [OrdersViewModel]'s single state object (docs/03-android-architecture.md
 * §2: one `StateFlow<UiState>` per screen).
 *
 * [rows]/[totalCount] map onto `device_orders_list` / `device_order_row_$index`
 * (`list` / `list_item`).
 */
public data class OrdersUiState(
    public val isLoading: Boolean = true,
    public val rows: List<OrderRowUiModel> = emptyList(),
    public val totalCount: Int = 0,
)

/**
 * One `device_order_row_$index` — exactly two text runs ([label], [value]),
 * the identical list/list_item contiguity constraint [SettlementRowUiModel]'s
 * kdoc documents in full. [value] folds the order's status (and courier, when
 * present) into plain text rather than a separate `_status`-suffixed child.
 */
public data class OrderRowUiModel(
    public val orderId: String,
    public val label: String,
    public val value: String,
)
