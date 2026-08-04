package com.vyaparpay.core.network

import java.io.IOException
import javax.inject.Inject
import kotlinx.serialization.json.Json
import okhttp3.HttpUrl
import okhttp3.Interceptor
import okhttp3.Response

/**
 * The interceptor [ApiErrorReporter]'s docstring promised: "the interceptor
 * that calls this lands with the Retrofit stack." Added to the shared
 * [okhttp3.OkHttpClient] in `di/NetworkModule.kt`, so every non-2xx response
 * any [VyaparApi] call receives is also written onto the `api_error` half of
 * the event timeline (docs/08 §2.1).
 *
 * **Scope.** This observes the *transport-level* verdict — a non-2xx HTTP
 * status — not the docs/13 §1 body-level verdict [EnvelopeAdapter] applies
 * (a 200 whose body says `"success": false"` is the "mangled status" case
 * `EnvelopeAdapter` alone handles). Duplicating that body-aware verdict logic
 * here would mean inventing a second mapping rule instead of reusing the
 * existing one; the docs/08 §2.1 taxonomy itself only ever cites the
 * transport-level case ("any non-2xx") for `api_error`, so this interceptor
 * stays at that layer.
 *
 * **Error-code reuse.** [decodeWireError] is the same parse [EnvelopeAdapter]
 * uses — this interceptor does not invent a second way to read `error.code`
 * off the wire.
 *
 * **Body handling.** [Response.peekBody] reads a bounded copy of the body
 * without consuming the real one, so [EnvelopeAdapter]'s own parse downstream
 * sees an untouched stream — this interceptor is a pure observer.
 */
public class ApiErrorReportingInterceptor @Inject constructor(
    private val reporter: ApiErrorReporter,
    private val json: Json,
) : Interceptor {

    override fun intercept(chain: Interceptor.Chain): Response {
        val request = chain.request()
        val response = chain.proceed(request)

        if (!response.isSuccessful) {
            reporter.report(
                error = mappedErrorCode(response),
                method = request.method,
                path = request.url.shortPath(),
                status = response.code,
            )
        }

        return response
    }

    private fun mappedErrorCode(response: Response): ApiError {
        val raw = try {
            response.peekBody(MAX_PEEK_BYTES).string()
        } catch (e: IOException) {
            null
        }
        return decodeWireError(json, raw).toApiError()
    }

    /**
     * `/v1/sessions` -> `/sessions` — the short form docs/08 §2.4's worked
     * example and [VyaparApi]'s own kdoc use (`"POST /sessions"`,
     * `"POST /payments"`), stripping the `/v1` prefix every base URL already
     * carries (docs/13 §1).
     */
    private fun HttpUrl.shortPath(): String {
        val segments = pathSegments.filterNot { it.isEmpty() }
        val withoutApiVersion = if (segments.firstOrNull() == API_VERSION_SEGMENT) segments.drop(1) else segments
        return "/" + withoutApiVersion.joinToString("/")
    }

    private companion object {
        private const val API_VERSION_SEGMENT = "v1"

        /** Generous enough for any docs/13 §1 error envelope; bounded so a huge body is never buffered whole. */
        private const val MAX_PEEK_BYTES = 16L * 1024
    }
}
