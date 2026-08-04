package com.vyaparpay.feature.dashboard

import com.vyaparpay.core.network.ApiResult
import java.util.Locale

private const val PAISE_PER_RUPEE = 100L

/**
 * `DashboardScreen`'s data seam (docs/03-android-architecture.md §2.1
 * repository pattern) -- the same shape as `PaymentRepository`: a
 * `suspend fun` returning [ApiResult], so [DashboardViewModel] does not
 * change shape when a Retrofit-backed implementation lands behind real
 * `/v1/wallet` + `/v1/settlements` calls (docs/13-api-contracts.md §3.1,
 * §3.3).
 */
public interface DashboardRepository {

    /** Fetches the merchant home snapshot: wallet balance, today's collections, next settlement, and any alert. */
    public suspend fun getDashboard(): ApiResult<DashboardSnapshot>
}

/** The successful outcome of [DashboardRepository.getDashboard]. */
public data class DashboardSnapshot(
    public val walletBalancePaise: Long,
    public val collectionsTodayPaise: Long,
    public val settlementEtaLabel: String,
    public val alertMessage: String?,
)

/**
 * The seeded stand-in for `DashboardScreen`'s content (docs/03 §4's worked
 * table: "Wallet balance (₹18,450), today's collections, quick actions, alert
 * banners").
 *
 * [WALLET_BALANCE_PAISE] mirrors `backend/scripts/seed.py`'s
 * `WALLET_BALANCE_PAISE` (line ~77) verbatim -- the same ₹18,450 figure the
 * canonical incident narrates (docs/01 §6 persona table). [COLLECTIONS_TODAY_PAISE]
 * and [KYC_ALERT_MESSAGE] are this screen's own demo fixtures, independent of
 * `SeededPaymentRepository`'s daily-limit constants -- this is a different
 * screen's numbers, not a reuse of the payment-decline incident's.
 */
public class SeededDashboardRepository : DashboardRepository {

    override suspend fun getDashboard(): ApiResult<DashboardSnapshot> = ApiResult.Success(
        DashboardSnapshot(
            walletBalancePaise = WALLET_BALANCE_PAISE,
            collectionsTodayPaise = COLLECTIONS_TODAY_PAISE,
            settlementEtaLabel = SETTLEMENT_ETA_LABEL,
            alertMessage = KYC_ALERT_MESSAGE,
        ),
    )

    public companion object {
        /** ₹18,450 -- `backend/scripts/seed.py`'s `WALLET_BALANCE_PAISE` (docs/01 §6 persona table). */
        public const val WALLET_BALANCE_PAISE: Long = 18_450_00L

        /**
         * Demo fixture, not derived from any backend seed value: 168 QR/UPI
         * collections today -- within docs/01 §6's "120-180 QR
         * collections/day" range -- averaging ₹85 each, ~₹14,280 total.
         */
        public const val COLLECTIONS_TODAY_PAISE: Long = 14_280_00L

        /** docs/03 §4's own worked example for this screen's `status_badge`. */
        public const val SETTLEMENT_ETA_LABEL: String = "Today, 6:00 PM"

        /** Demo fixture: a generic KYC nudge, independent of any payment-decline incident. */
        public const val KYC_ALERT_MESSAGE: String =
            "Complete your KYC verification to avoid limits on your wallet."
    }
}

/**
 * Formats whole-rupee paise as `"₹18,450"`. Comma grouping is pinned to
 * [Locale.US] so the separator is deterministic under Robolectric regardless
 * of the JVM's default locale -- and matches the grouping docs/03 §4's own
 * `₹18,450` example uses.
 */
internal fun formatRupees(paise: Long): String =
    "₹" + String.format(Locale.US, "%,d", paise / PAISE_PER_RUPEE)
