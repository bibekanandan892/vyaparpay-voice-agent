package com.vyaparpay.feature.payments

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.vyaparpay.core.analytics.EventTracker
import com.vyaparpay.core.network.ApiResult
import dagger.hilt.android.lifecycle.HiltViewModel
import javax.inject.Inject
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

private val MONTH_ABBREVIATIONS = arrayOf(
    "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)

/**
 * Drives `SettlementsScreen` — loads the settlement-batches page once on
 * construction (there is no user-triggered reload path on this screen yet,
 * matching the read-only `GET /v1/settlements` this stands in for, docs/13
 * §3.3) and derives the header balance/status/alert fields from whatever the
 * repository returns.
 *
 * **Now `@HiltViewModel` (Phase-4 T8a)**, for the identical reason
 * [PaymentViewModel] gives in its own kdoc: [SettlementsRepository] still has
 * no Hilt module (repository seeding is unchanged, out of scope), but
 * [EventTracker] does now. See [PaymentViewModel]'s `@Inject` constructor
 * kdoc for why that constructor takes only [events], not [settlements] — and
 * for why its delegation below spells out [SeededSettlementsRepository]
 * explicitly rather than writing `this(events = events)` (a genuine
 * self-delegation ambiguity Kotlin rejects at compile time otherwise).
 */
@HiltViewModel
public class SettlementsViewModel @JvmOverloads constructor(
    private val settlements: SettlementsRepository = SeededSettlementsRepository(),
    public val events: EventTracker = InMemoryEventTracker(),
) : ViewModel() {

    @Inject
    public constructor(events: EventTracker) : this(settlements = SeededSettlementsRepository(), events = events)

    private val _state = MutableStateFlow(SettlementsUiState())
    public val state: StateFlow<SettlementsUiState> = _state.asStateFlow()

    init {
        load()
    }

    /**
     * `export_csv_secondary` tapped. No CSV export pipeline exists yet (no
     * `VyaparApi` binding — see this class's own kdoc); the tap itself is
     * already recorded on the timeline by `trackedClickable` at the call
     * site, the same division of responsibility [PaymentViewModel.onPayNowClicked]'s
     * kdoc describes. Left as a real function reference (not a lambda
     * literal at the call site) so `SettlementsScreen` has something stable
     * to wire and this method's body is a one-line change once export lands.
     */
    public fun onExportCsvClicked() {
        // No-op today — see kdoc above.
    }

    private fun load() {
        viewModelScope.launch {
            when (val result = settlements.getSettlements(limit = PAGE_LIMIT)) {
                is ApiResult.Success -> onLoaded(result.data)
                is ApiResult.Failure -> _state.update { it.copy(isLoading = false) }
            }
        }
    }

    private fun onLoaded(page: SettlementsPage) {
        val netSum = page.items.sumOf(Settlement::netPaise)
        val feesSum = page.items.sumOf(Settlement::feesPaise)
        val latest = page.items.firstOrNull()
        val shortfallCount = page.items.count {
            it.status == SettlementStatus.FAILED || it.status == SettlementStatus.PARTIAL
        }
        _state.update {
            it.copy(
                isLoading = false,
                netSettledValue = formatIndianRupees(netSum),
                feesValue = formatIndianRupees(feesSum),
                latestBatchStatus = latest?.status?.name.orEmpty(),
                shortfallMessage = shortfallCount.takeIf { count -> count > 0 }?.let { count ->
                    "$count batch${if (count == 1) "" else "es"} flagged for shortfall"
                },
                rows = page.items.map { it.toRowUiModel() },
                totalCount = page.totalCount,
            )
        }
    }

    private companion object {
        /** Comfortably above [SeededSettlementsRepository.TOTAL_COUNT] so the header/list always see every seeded row. */
        const val PAGE_LIMIT = 100

        fun Settlement.toRowUiModel(): SettlementRowUiModel = SettlementRowUiModel(
            settlementId = settlementId,
            label = formatBatchDateLabel(batchDate),
            // Status folded into plain text, deliberately NOT a second
            // `_status`-suffixed node — see SettlementRowUiModel's own kdoc.
            value = "${formatIndianRupees(netPaise)} · ${status.name}",
        )

        fun formatBatchDateLabel(isoDate: String): String {
            val (year, month, day) = isoDate.split("-")
            return "${day.toInt()} ${MONTH_ABBREVIATIONS[month.toInt() - 1]} $year"
        }

        fun formatIndianRupees(paise: Long): String {
            val rupees = paise / 100
            val paiseRemainder = paise % 100
            return "₹${groupIndian(rupees.toString())}.${paiseRemainder.toString().padStart(2, '0')}"
        }

        /** Indian digit grouping — last 3 digits, then groups of 2 (e.g. "811370" -> "8,11,370"). */
        fun groupIndian(digits: String): String {
            if (digits.length <= 3) return digits
            val tail = digits.takeLast(3)
            var head = digits.dropLast(3)
            val headGroups = mutableListOf<String>()
            while (head.length > 2) {
                headGroups.add(0, head.takeLast(2))
                head = head.dropLast(2)
            }
            if (head.isNotEmpty()) headGroups.add(0, head)
            return (headGroups + tail).joinToString(",")
        }
    }
}
