package com.vyaparpay.feature.support

import com.vyaparpay.core.network.ApiResult

/**
 * `HelpScreen`'s data seam (docs/03-android-architecture.md §2.1 repository
 * pattern) -- FAQ entries and the merchant's own complaint history, the two
 * informational roles docs/01-product-and-use-case.md §5 assigns this screen
 * (`faq_item`, `complaint_row`).
 */
public interface SupportRepository {

    /** Fetches the FAQ hub and the merchant's complaint history for `HelpScreen`. */
    public suspend fun getHelpContent(): ApiResult<HelpContent>
}

/** The successful outcome of [SupportRepository.getHelpContent]. */
public data class HelpContent(
    public val faqs: List<FaqEntry>,
    public val complaints: List<ComplaintSummary>,
)

/** One FAQ entry. */
public data class FaqEntry(
    public val question: String,
    public val answer: String,
)

/**
 * One row of the merchant's complaint history -- the
 * docs/12-data-models.md §3.4 `complaints` table shape, trimmed to what
 * `HelpScreen` renders (no `session_id`/`sla_due_at`/timestamps, which this
 * screen does not show).
 */
public data class ComplaintSummary(
    public val complaintId: String,
    public val category: String,
    public val subject: String,
    public val status: String,
)

/**
 * The seeded stand-in for `HelpScreen`'s FAQ hub and complaint history.
 *
 * The first complaint is the canonical `cmp_0724_0031` / "T+1 settlement not
 * credited" / `open` example verbatim from docs/13-api-contracts.md §3.4
 * (`POST /v1/complaints`'s own worked response); the other two follow the
 * same `cmp_MMDD_NNNN` id shape and the docs/12 §3.4 category/status
 * vocabulary, invented for demo variety.
 */
public class SeededSupportRepository : SupportRepository {

    override suspend fun getHelpContent(): ApiResult<HelpContent> = ApiResult.Success(
        HelpContent(faqs = SEEDED_FAQS, complaints = SEEDED_COMPLAINTS),
    )

    public companion object {
        public val SEEDED_FAQS: List<FaqEntry> = listOf(
            FaqEntry(
                question = "Why was my payment declined?",
                answer = "Your payment may be declined if it would push today's bank " +
                    "transaction usage over your daily limit, or if your wallet " +
                    "balance is insufficient. Check the reason shown on the Payment " +
                    "screen for the exact cause.",
            ),
            FaqEntry(
                question = "When will my settlement arrive?",
                answer = "Settlements are processed T+1 -- funds from today's " +
                    "collections typically land in your bank account by the " +
                    "evening of the next business day.",
            ),
            FaqEntry(
                question = "How do I track my soundbox or QR kit order?",
                answer = "Open Device Orders from the dashboard to see live courier " +
                    "tracking for any order you've placed.",
            ),
            FaqEntry(
                question = "How do I raise a complaint?",
                answer = "Tap Call Support below to talk it through, or the agent " +
                    "can open a complaint ticket for you and share the reference id.",
            ),
            FaqEntry(
                question = "Can I increase my daily transaction limit?",
                answer = "Yes -- request a limit increase from the Payment screen " +
                    "after a decline. Approvals typically complete within 4 " +
                    "business hours.",
            ),
            FaqEntry(
                question = "Is my card and PIN information safe?",
                answer = "VyaparPay never stores your full card number. Card " +
                    "controls -- block card, reset PIN -- are available from your " +
                    "wallet card details.",
            ),
        )

        public val SEEDED_COMPLAINTS: List<ComplaintSummary> = listOf(
            // Canonical example, docs/13 §3.4's own worked POST /v1/complaints response.
            ComplaintSummary(
                complaintId = "cmp_0724_0031",
                category = "settlement",
                subject = "T+1 settlement not credited",
                status = "open",
            ),
            ComplaintSummary(
                complaintId = "cmp_0715_0022",
                category = "device",
                subject = "Soundbox not received",
                status = "in_progress",
            ),
            ComplaintSummary(
                complaintId = "cmp_0701_0007",
                category = "refund",
                subject = "Refund to customer stuck",
                status = "resolved",
            ),
        )
    }
}
