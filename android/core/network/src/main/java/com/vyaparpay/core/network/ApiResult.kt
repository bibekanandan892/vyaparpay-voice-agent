package com.vyaparpay.core.network

/**
 * What every repository returns (docs/03-android-architecture.md §2.1).
 *
 * The point of this type is what it *hides*: no ViewModel ever sees an HTTP
 * status code or a raw `{success, data, error, meta}` envelope. Unwrapping
 * happens once, in the shared envelope adapter, and everything above this
 * boundary reasons about [ApiError] instead.
 */
public sealed interface ApiResult<out T> {

    public data class Success<out T>(val data: T) : ApiResult<T>

    public data class Failure(
        val code: ApiError,
        val message: String,
    ) : ApiResult<Nothing>
}
