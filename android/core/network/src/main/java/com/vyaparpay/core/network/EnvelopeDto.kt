package com.vyaparpay.core.network

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject

/**
 * The one REST envelope every endpoint wears (docs/13 §1):
 * `{success, data, error, meta}`.
 *
 * Internal on purpose — docs/03 §2.1: "no ViewModel ever sees an HTTP status
 * code or a raw envelope". [EnvelopeAdapter] unwraps this into [ApiResult]
 * and nothing above that boundary imports it.
 *
 * Every property has a lenient default so a partial or hostile body *decodes*
 * and then *fails the verdict check* in the adapter, instead of throwing —
 * the docs/13 §9 fold-don't-crash posture applied one level below unknown
 * keys. `meta` (pagination on list endpoints) is deliberately not modelled
 * yet: none of the session endpoints carry it, and `ignoreUnknownKeys`
 * already tolerates its presence; the typed field lands with the first
 * paginated repository.
 */
@Serializable
internal data class EnvelopeDto<T>(
    /** `true` iff HTTP 2xx — the body carries its own verdict because mobile stacks mangle status codes (docs/13 §1). */
    @SerialName("success") val success: Boolean = false,
    @SerialName("data") val data: T? = null,
    @SerialName("error") val error: WireErrorDto? = null,
)

/** The envelope's `error` object: `{code, message, details?}` (docs/13 §1). */
@Serializable
internal data class WireErrorDto(
    /** A stable machine string from the §1.1 taxonomy — mapped, never surfaced raw. */
    @SerialName("code") val code: String? = null,
    /** Human-readable and safe to show in a snackbar (docs/13 §1). */
    @SerialName("message") val message: String? = null,
    /** Optional structured context, passed through to [ApiResult.Failure.details]. */
    @SerialName("details") val details: JsonObject? = null,
)
