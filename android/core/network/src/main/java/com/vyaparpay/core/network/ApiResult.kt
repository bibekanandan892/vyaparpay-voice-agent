package com.vyaparpay.core.network

import kotlinx.serialization.json.JsonObject

/**
 * What every repository returns (docs/03-android-architecture.md §2.1).
 *
 * The point of this type is what it *hides*: no ViewModel ever sees an HTTP
 * status code or a raw `{success, data, error, meta}` envelope. Unwrapping
 * happens once, in the shared [EnvelopeAdapter], and everything above this
 * boundary reasons about [ApiError] instead.
 */
public sealed interface ApiResult<out T> {

    public data class Success<out T>(val data: T) : ApiResult<T>

    /**
     * A failed call — a non-2xx envelope, a body-level `success: false`
     * verdict, a malformed response, or a transport failure that never
     * reached the server.
     *
     * @param code the mapped docs/13 §1.1 taxonomy member; anything the
     *   build does not recognise (and every transport failure) folds to
     *   [ApiError.UNKNOWN].
     * @param message human-readable and safe to show in a snackbar
     *   (docs/13 §1 makes that a property of the wire `error.message`;
     *   locally produced fallbacks keep the same promise).
     * @param details the envelope's optional structured context — e.g. the
     *   limit arithmetic behind a 402, or `existing_request_id` on a 409
     *   (docs/13 §3.2, §3.4). `null` when the server sent none or the body
     *   never parsed.
     */
    public data class Failure(
        val code: ApiError,
        val message: String,
        val details: JsonObject? = null,
    ) : ApiResult<Nothing>
}
