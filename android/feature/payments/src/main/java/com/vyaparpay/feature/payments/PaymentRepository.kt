package com.vyaparpay.feature.payments

import com.vyaparpay.core.network.ApiError
import com.vyaparpay.core.network.ApiResult

/**
 * The pay-a-vendor seam (docs/03-android-architecture.md §2.1 repository
 * pattern).
 *
 * `VyaparApi` (`:core:network`) has no `/payments` endpoint yet and `:app`'s
 * Hilt graph binds no `VyaparApi` at all — the demo payment rail is scripted
 * decline codes served from the backend seed (docs/01-product-and-use-case.md
 * §5 demo/production table), not a route this client calls today. This
 * interface is the seam a Retrofit-backed implementation drops into once that
 * lands, shaped exactly like every other repository in `:core:network`
 * (`suspend fun` returning [ApiResult]) so [PaymentViewModel] does not change
 * shape when that happens — only its constructor argument does.
 */
public interface PaymentRepository {

    /** Attempts to pay [recipient] [amountPaise] paise from the wallet. */
    public suspend fun payVendor(amountPaise: Long, recipient: String): ApiResult<PaymentReceipt>
}

/** The successful outcome of [PaymentRepository.payVendor]. */
public data class PaymentReceipt(
    public val paymentId: String,
    public val recipient: String,
    public val amountPaise: Long,
)

/**
 * The seeded stand-in for the canonical Rajesh Kumar / ₹245 / `Amazon
 * Business` / `DAILY_LIMIT_EXCEEDED` incident
 * (docs/01-product-and-use-case.md §7 step 2, §8 turn 1, §9 "Decisions this
 * document exports"; the same fixture `backend/scripts/seed.py` seeds
 * server-side).
 *
 * This is a fixed Merchant Pro daily-limit fixture, not a general rules
 * engine: any payment that would push today's bank-limit usage over the
 * ceiling is declined — exactly the arithmetic the canon narrates (₹24,890 of
 * ₹25,000 already used, so any payment over ₹110 fails until midnight).
 */
public class SeededPaymentRepository : PaymentRepository {

    override suspend fun payVendor(amountPaise: Long, recipient: String): ApiResult<PaymentReceipt> {
        val projectedUsagePaise = DAILY_LIMIT_USED_PAISE + amountPaise
        return if (projectedUsagePaise > DAILY_BANK_LIMIT_PAISE) {
            ApiResult.Failure(
                code = ApiError.DAILY_LIMIT_EXCEEDED,
                message = "Your daily transaction limit has been reached. " +
                    "Try again tomorrow, or contact support for a limit increase.",
            )
        } else {
            ApiResult.Success(
                PaymentReceipt(
                    paymentId = "pay_${System.nanoTime()}",
                    recipient = recipient,
                    amountPaise = amountPaise,
                ),
            )
        }
    }

    public companion object {
        /** Rajesh's bank daily transaction limit (docs/01 §6 persona table). */
        public const val DAILY_BANK_LIMIT_PAISE: Long = 25_000_00L

        /** Already used today, before this payment (docs/01 §7 step 2, §8 turn 3). */
        public const val DAILY_LIMIT_USED_PAISE: Long = 24_890_00L

        /** The canonical vendor-payment amount (docs/01 §7 step 1). */
        public const val CANONICAL_AMOUNT_PAISE: Long = 245_00L

        /** The canonical payee — Rajesh's own shop is "Kumar General Store"; this is who *he* pays (docs/01 §7, §8, §9). */
        public const val CANONICAL_RECIPIENT: String = "Amazon Business"
    }
}
