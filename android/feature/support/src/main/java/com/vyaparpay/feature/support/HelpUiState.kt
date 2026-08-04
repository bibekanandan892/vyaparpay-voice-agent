package com.vyaparpay.feature.support

/**
 * [HelpViewModel]'s single state object (docs/03-android-architecture.md §2:
 * one `StateFlow<UiState>` per screen, unidirectional data flow).
 *
 * Role fidelity is not load-bearing here -- `HelpScreen` is excluded from
 * capture (`SupportDestination.IS_CAPTURED == false`) -- but the shape still
 * follows the same state-down/events-up seam every other screen does.
 * [faqs] backs `faq_list`/`faq_item_$i`; [complaints] backs
 * `complaint_row_$i` (docs/01-product-and-use-case.md §5's `HelpScreen` row:
 * "FAQ, complaint status, Call Support entry point").
 */
public data class HelpUiState(
    public val isLoading: Boolean = true,
    public val faqs: List<FaqEntry> = emptyList(),
    public val complaints: List<ComplaintSummary> = emptyList(),
)
