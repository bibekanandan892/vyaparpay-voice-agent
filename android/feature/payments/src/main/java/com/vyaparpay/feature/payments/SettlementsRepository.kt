package com.vyaparpay.feature.payments

import com.vyaparpay.core.network.ApiResult

/**
 * The settlement-batches seam (docs/03-android-architecture.md §2.1
 * repository pattern) — `GET /v1/settlements` (docs/13-api-contracts.md
 * §3.3) has no client-side caller yet and `:app`'s Hilt graph binds no
 * `VyaparApi`, so [SeededSettlementsRepository] stands in with the same
 * canonical rows docs/13 §3.3 and the backend seed serve, exactly like
 * [SeededPaymentRepository] does for the vendor-payment flow.
 */
public interface SettlementsRepository {

    /** Fetches up to [limit] settlement batches, newest first. */
    public suspend fun getSettlements(limit: Int): ApiResult<SettlementsPage>
}

/**
 * One page of settlement batches. [totalCount] is the server-reported total
 * across every batch, independent of how many [items] this page carries —
 * mirrors docs/13 §3.3's `meta.total` being 34 while a `?limit=2` call
 * returns only two `items`.
 */
public data class SettlementsPage(
    public val items: List<Settlement>,
    public val totalCount: Int,
)

/** Mirrors docs/12-data-models.md §3.4's `settlements.status` CHECK constraint, one member per allowed value. */
public enum class SettlementStatus {
    PROCESSING,
    SETTLED,
    FAILED,
    PARTIAL,
}

/**
 * One settlement batch row (docs/12 §3.4's `settlements` DDL).
 *
 * [netPaise] must always equal [grossPaise] minus [feesPaise] — the same
 * `CHECK (net_paise = gross_paise - fees_paise)` constraint the table itself
 * enforces server-side. Enforced here with `require` so a bad seed value
 * fails loudly the moment it is constructed, rather than silently drifting
 * from the invariant the way a plain data-class field could.
 */
public data class Settlement(
    public val settlementId: String,
    /** ISO `yyyy-MM-dd`. No date library is on this module's classpath (android/gradle/libs.versions.toml carries no kotlinx-datetime pin), and `java.time` needs API 26+ or core-library desugaring, neither configured here — a plain ISO string is deterministic, sortable, and sufficient for seed data that is never parsed as user input. */
    public val batchDate: String,
    public val grossPaise: Long,
    public val feesPaise: Long,
    public val netPaise: Long,
    public val status: SettlementStatus,
    /** The T+1 settlement promise; `null` once [status] is [SettlementStatus.SETTLED] (docs/12 §3.4's own `expected_by` comment). */
    public val expectedBy: String?,
    /** Bank UTR reference; `null` until [status] is [SettlementStatus.SETTLED] (docs/12 §3.4). */
    public val utr: String?,
) {
    init {
        require(netPaise == grossPaise - feesPaise) {
            "netPaise ($netPaise) must equal grossPaise ($grossPaise) - feesPaise ($feesPaise) for $settlementId"
        }
    }
}

/**
 * Seeds the two canonical rows from docs/13-api-contracts.md §3.3 verbatim,
 * then generates 32 more DETERMINISTICALLY — a pure function of index, no
 * [kotlin.random.Random] and no wall-clock read, so a test asserting against
 * this repository's output never flakes and two instances always agree
 * byte-for-byte.
 *
 * **Doc discrepancy, not fixed here.** [TOTAL_COUNT] is **34** — docs/03 §4's
 * demo-screens table ("long list (34 batches)") and docs/13 §3.3's
 * `meta.total` both agree on that figure. This is deliberately NOT the `47`
 * that `protocol/fixtures/context/screen_context_settlements_dense.json`
 * uses for its `list.total_count` value: that fixture is a
 * `:core:screencontext` design-spec illustration of the IR *shape* (list
 * contiguity, the 20-component cap) for a hypothetical dense screen, not a
 * source of truth for this repository's row count — and this task was
 * explicitly told not to edit `protocol/`. Two independent docs agree on 34,
 * so that is what this repository seeds.
 */
public class SeededSettlementsRepository : SettlementsRepository {

    override suspend fun getSettlements(limit: Int): ApiResult<SettlementsPage> =
        ApiResult.Success(SettlementsPage(items = ALL_SETTLEMENTS.take(limit), totalCount = TOTAL_COUNT))

    public companion object {
        public const val TOTAL_COUNT: Int = 34

        /** docs/13 §3.3's first canonical row, verbatim. */
        private val CANONICAL_LATEST = Settlement(
            settlementId = "setl_0723_t1",
            batchDate = "2026-07-23",
            grossPaise = 8_123_400L,
            feesPaise = 9_700L,
            netPaise = 8_113_700L,
            status = SettlementStatus.SETTLED,
            expectedBy = null,
            utr = "UTR2607231234",
        )

        /** docs/13 §3.3's second canonical row, verbatim. */
        private val CANONICAL_PREVIOUS = Settlement(
            settlementId = "setl_0722_t1",
            batchDate = "2026-07-22",
            grossPaise = 6_540_000L,
            feesPaise = 7_800L,
            netPaise = 6_532_200L,
            status = SettlementStatus.SETTLED,
            expectedBy = null,
            utr = "UTR2607221198",
        )

        /**
         * Rows 3..34 — one calendar day older than the previous row for
         * each step back from 2026-07-21, deterministic pure function of
         * [index] alone. Status cycles on fixed moduli so the batch list is
         * never monotonically SETTLED, giving the header's shortfall alert
         * (`shortfall_alert`) something real to report.
         */
        private fun generated(index: Int): Settlement {
            val batchDate = addDays("2026-07-21", -(index - 3).toLong())
            val status = when {
                index % 11 == 0 -> SettlementStatus.FAILED
                index % 7 == 0 -> SettlementStatus.PARTIAL
                index % 13 == 0 -> SettlementStatus.PROCESSING
                else -> SettlementStatus.SETTLED
            }
            val isSettled = status == SettlementStatus.SETTLED
            val grossPaise = 3_000_00L + index * 211_00L
            val feesPaise = (grossPaise * 12L) / 1_000L // ~1.2%, matching the canonical rows' rough fee ratio
            val netPaise = grossPaise - feesPaise
            val yymmdd = batchDate.replace("-", "").substring(2)
            return Settlement(
                settlementId = "setl_gen_%02d".format(index),
                batchDate = batchDate,
                grossPaise = grossPaise,
                feesPaise = feesPaise,
                netPaise = netPaise,
                status = status,
                expectedBy = if (isSettled) null else "${addDays(batchDate, 1)}T18:00:00+05:30",
                utr = if (isSettled) "UTR$yymmdd%04d".format(index) else null,
            )
        }

        private val ALL_SETTLEMENTS: List<Settlement> =
            listOf(CANONICAL_LATEST, CANONICAL_PREVIOUS) + (3..TOTAL_COUNT).map(::generated)

        // -- Deterministic proleptic-Gregorian date arithmetic -------------------
        //
        // Howard Hinnant's days_from_civil / civil_from_days algorithm (pure
        // integer math, no calendar library). Needed only because no date
        // library is on this module's classpath and java.time is unavailable
        // below API 26 without desugaring (neither configured here) — see
        // [Settlement.batchDate]'s own kdoc. Correct for any proleptic-
        // Gregorian date, well beyond this seed's 2026 range.

        private fun daysFromCivil(y: Int, m: Int, d: Int): Long {
            val yAdj = (if (m <= 2) y - 1 else y).toLong()
            val era = (if (yAdj >= 0) yAdj else yAdj - 399) / 400
            val yoe = yAdj - era * 400
            val mp = (m + 9) % 12
            val doy = (153 * mp + 2) / 5 + d - 1
            val doe = yoe * 365 + yoe / 4 - yoe / 100 + doy
            return era * 146_097L + doe - 719_468L
        }

        private fun civilFromDays(z: Long): Triple<Int, Int, Int> {
            val zAdj = z + 719_468L
            val era = (if (zAdj >= 0) zAdj else zAdj - 146_096) / 146_097
            val doe = zAdj - era * 146_097
            val yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365
            val y = yoe + era * 400
            val doy = doe - (365 * yoe + yoe / 4 - yoe / 100)
            val mp = (5 * doy + 2) / 153
            val d = (doy - (153 * mp + 2) / 5 + 1).toInt()
            val m = (if (mp < 10) mp + 3 else mp - 9).toInt()
            val yFinal = if (m <= 2) y + 1 else y
            return Triple(yFinal.toInt(), m, d)
        }

        private fun addDays(isoDate: String, delta: Long): String {
            val parts = isoDate.split("-")
            val epoch = daysFromCivil(parts[0].toInt(), parts[1].toInt(), parts[2].toInt()) + delta
            val (y, m, d) = civilFromDays(epoch)
            return "%04d-%02d-%02d".format(y, m, d)
        }
    }
}
