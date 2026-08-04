package com.vyaparpay.core.network

import java.io.IOException
import java.lang.reflect.ParameterizedType
import java.lang.reflect.Type
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import okhttp3.Request
import okio.Timeout
import retrofit2.Call
import retrofit2.CallAdapter
import retrofit2.Callback
import retrofit2.Response
import retrofit2.Retrofit

/**
 * The single response-envelope adapter every repository shares
 * (docs/03 §2.1, module table §1: `EnvelopeAdapter`).
 *
 * It teaches Retrofit the return type `ApiResult<T>`: the converter parses
 * the docs/13 §1 envelope, and this adapter reads `{success, data, error}`,
 * returns `data` on success, and maps `error.code` onto [ApiError] on
 * failure. Unknown codes fold to [ApiError.UNKNOWN] rather than crashing —
 * the docs/13 §9 client rule — and so does everything else that can go wrong
 * on a phone network: a malformed body, a mangled status, a timeout, a dead
 * radio. A repository call *returns*; it never throws wire trouble upward.
 */
internal class EnvelopeAdapter(private val json: Json) : CallAdapter.Factory() {

    override fun get(
        returnType: Type,
        annotations: Array<out Annotation>,
        retrofit: Retrofit,
    ): CallAdapter<*, *>? {
        if (getRawType(returnType) != Call::class.java) return null
        check(returnType is ParameterizedType) {
            "ApiResult return types must be parameterized, e.g. ApiResult<ConnectBundleDto>"
        }
        val resultType = getParameterUpperBound(0, returnType)
        if (getRawType(resultType) != ApiResult::class.java) return null
        check(resultType is ParameterizedType) {
            "ApiResult must be parameterized, e.g. ApiResult<ConnectBundleDto>"
        }
        val dataType = getParameterUpperBound(0, resultType)
        return Adapter<Any>(EnvelopeType(dataType), json)
    }

    private class Adapter<T>(
        private val envelopeType: Type,
        private val json: Json,
    ) : CallAdapter<EnvelopeDto<T>, Call<ApiResult<T>>> {
        override fun responseType(): Type = envelopeType
        override fun adapt(call: Call<EnvelopeDto<T>>): Call<ApiResult<T>> =
            EnvelopeCall(call, json)
    }

    /** `EnvelopeDto<data>` as a reflective [Type], so the kotlinx converter parses the wrapper, not the bare DTO. */
    private class EnvelopeType(private val dataType: Type) : ParameterizedType {
        override fun getRawType(): Type = EnvelopeDto::class.java
        override fun getActualTypeArguments(): Array<Type> = arrayOf(dataType)
        override fun getOwnerType(): Type? = null
    }
}

/** Snackbar-safe copy for a failure that never produced a wire envelope (docs/13 §1: message is user-showable). */
private const val TRANSPORT_FAILURE_MESSAGE =
    "Couldn't reach VyaparPay. Check your connection and try again."
private const val MALFORMED_RESPONSE_MESSAGE =
    "Something went wrong talking to VyaparPay. Please try again."

/**
 * The adapted call: delegates the HTTP work, then folds the outcome —
 * success, wire error, malformed body, or transport failure — into a single
 * [ApiResult] delivered through `onResponse`. `onFailure` is never
 * propagated, which is what lets `suspend fun … : ApiResult<T>` signatures
 * return instead of throw.
 */
private class EnvelopeCall<T>(
    private val delegate: Call<EnvelopeDto<T>>,
    private val json: Json,
) : Call<ApiResult<T>> {

    override fun enqueue(callback: Callback<ApiResult<T>>) {
        delegate.enqueue(
            object : Callback<EnvelopeDto<T>> {
                override fun onResponse(call: Call<EnvelopeDto<T>>, response: Response<EnvelopeDto<T>>) {
                    callback.onResponse(this@EnvelopeCall, Response.success(fold(response)))
                }

                override fun onFailure(call: Call<EnvelopeDto<T>>, t: Throwable) {
                    callback.onResponse(this@EnvelopeCall, Response.success(foldThrowable(t)))
                }
            },
        )
    }

    override fun execute(): Response<ApiResult<T>> {
        val result = try {
            fold(delegate.execute())
        } catch (e: IOException) {
            foldThrowable(e)
        } catch (e: RuntimeException) {
            // Retrofit wraps converter failures for synchronous calls too.
            foldThrowable(e)
        }
        return Response.success(result)
    }

    /**
     * The docs/13 §1 verdict, in trust order: a 2xx body's own `success`
     * flag wins over the status code (the "mobile stacks mangle status
     * codes" rule), the error body is parsed on non-2xx, and anything that
     * fits neither shape folds to the safe [ApiError.UNKNOWN] failure.
     */
    private fun fold(response: Response<EnvelopeDto<T>>): ApiResult<T> {
        if (!response.isSuccessful) return foldErrorBody(response)
        val body = response.body() ?: return malformedFailure()
        val data = body.data
        return when {
            body.success && data != null -> ApiResult.Success(data)
            body.error != null -> body.error.toFailure()
            else -> malformedFailure()
        }
    }

    private fun foldErrorBody(response: Response<EnvelopeDto<T>>): ApiResult.Failure {
        val raw = try {
            response.errorBody()?.string()
        } catch (e: IOException) {
            null
        }
        return decodeWireError(json, raw)?.toFailure() ?: malformedFailure()
    }

    private fun WireErrorDto.toFailure(): ApiResult.Failure = ApiResult.Failure(
        code = ApiError.fromWireCode(code),
        message = message ?: MALFORMED_RESPONSE_MESSAGE,
        details = details,
    )

    private fun foldThrowable(t: Throwable): ApiResult.Failure = ApiResult.Failure(
        code = ApiError.UNKNOWN,
        message = if (t is IOException) TRANSPORT_FAILURE_MESSAGE else MALFORMED_RESPONSE_MESSAGE,
    )

    private fun malformedFailure(): ApiResult.Failure =
        ApiResult.Failure(code = ApiError.UNKNOWN, message = MALFORMED_RESPONSE_MESSAGE)

    override fun clone(): Call<ApiResult<T>> = EnvelopeCall(delegate.clone(), json)
    override fun isExecuted(): Boolean = delegate.isExecuted
    override fun cancel(): Unit = delegate.cancel()
    override fun isCanceled(): Boolean = delegate.isCanceled
    override fun request(): Request = delegate.request()
    override fun timeout(): Timeout = delegate.timeout()
}

/**
 * Error bodies always carry `data: null` (docs/13 §1), so one fixed
 * serializer covers every endpoint's failure path; the [JsonObject]
 * parameter is never exercised.
 */
private val errorEnvelopeSerializer = EnvelopeDto.serializer(JsonObject.serializer())

/**
 * Parses a non-2xx response body's `error` object, folding a missing,
 * blank, or malformed body to `null` rather than throwing — the docs/13 §9
 * fold-don't-crash posture applied at the parse boundary.
 *
 * Shared by [EnvelopeCall] (folds to a full [ApiResult.Failure] with message
 * and details, for repository callers) and [ApiErrorReportingInterceptor]
 * (folds to just the [ApiError] code, for the timeline) — one parse, two
 * consumers, so the mapping can never drift between the two call sites.
 */
internal fun decodeWireError(json: Json, rawBody: String?): WireErrorDto? {
    if (rawBody.isNullOrBlank()) return null
    return try {
        json.decodeFromString(errorEnvelopeSerializer, rawBody).error
    } catch (e: SerializationException) {
        null
    } catch (e: IllegalArgumentException) {
        null
    }
}

/** Maps a parsed wire error onto [ApiError], folding a missing/absent error to [ApiError.UNKNOWN]. */
internal fun WireErrorDto?.toApiError(): ApiError = ApiError.fromWireCode(this?.code)
