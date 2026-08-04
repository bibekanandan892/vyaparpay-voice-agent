package com.vyaparpay.core.network

import com.vyaparpay.core.analytics.RingBufferEventTracker
import com.vyaparpay.core.network.di.NetworkModule
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.SocketPolicy
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * The failure half of the [EnvelopeAdapter] contract: every docs/13 §1.1
 * envelope maps onto the right [ApiError], and everything that is *not* a
 * well-formed envelope — garbage bodies, mangled statuses, dead sockets,
 * timeouts — folds to a safe [ApiResult.Failure] instead of throwing.
 */
class EnvelopeAdapterErrorTest {

    private lateinit var server: MockWebServer
    private lateinit var api: VyaparApi

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
        api = apiAgainstServer(defaultInterceptedClient())
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    private fun apiAgainstServer(client: OkHttpClient): VyaparApi =
        NetworkModule.provideVyaparApiFactory(client, NetworkModule.provideJson())
            .create(server.url("/v1/").toString())

    /** The interceptor-equipped default client, matching what `:app` actually gets from `NetworkModule`. */
    private fun defaultInterceptedClient(): OkHttpClient {
        val json = NetworkModule.provideJson()
        val interceptor = ApiErrorReportingInterceptor(ApiErrorReporter(RingBufferEventTracker()), json)
        return NetworkModule.provideOkHttpClient(interceptor)
    }

    private fun enqueueError(status: Int, body: String) {
        server.enqueue(MockResponse().setResponseCode(status).setBody(body))
    }

    private fun failureOf(result: ApiResult<*>): ApiResult.Failure {
        if (result !is ApiResult.Failure) {
            throw AssertionError("Expected ApiResult.Failure but was $result")
        }
        return result
    }

    // -- docs/13 §1.1 envelope mapping ----------------------------------------------

    @Test
    fun `a 402 with the canonical daily-limit body maps code, message, and details`() = runBlocking {
        enqueueError(
            402,
            """{"success": false, "data": null,
                "error": {"code": "DAILY_LIMIT_EXCEEDED",
                          "message": "Daily transaction limit exceeded: limit ₹25,000, ₹24,890 already used today.",
                          "details": {"txn_id": "txn_0724_1414a", "attempted_paise": 24500,
                                      "limit_paise": 2500000, "used_today_paise": 2489000,
                                      "resets_at": "2026-07-25T00:00:00+05:30"}},
                "meta": null}""",
        )

        val failure = failureOf(api.createSession(PHASE3_REQUEST))

        assertEquals(ApiError.DAILY_LIMIT_EXCEEDED, failure.code)
        assertTrue(failure.message.startsWith("Daily transaction limit exceeded"))
        assertEquals(2_500_000, failure.details?.get("limit_paise")?.jsonPrimitive?.int)
        assertEquals(2_489_000, failure.details?.get("used_today_paise")?.jsonPrimitive?.int)
    }

    @Test
    fun `a 404 SESSION_NOT_FOUND maps onto its taxonomy member`() = runBlocking {
        enqueueError(
            404,
            """{"success": false, "data": null,
                "error": {"code": "SESSION_NOT_FOUND", "message": "No session found for this id",
                          "details": {"session_id": "zzz999"}},
                "meta": null}""",
        )

        assertEquals(ApiError.SESSION_NOT_FOUND, failureOf(api.endSession("zzz999")).code)
    }

    @Test
    fun `a 404 SESSION_SUMMARY_PENDING is distinguished from SESSION_NOT_FOUND on the same status`() = runBlocking {
        enqueueError(
            404,
            """{"success": false, "data": null,
                "error": {"code": "SESSION_SUMMARY_PENDING",
                          "message": "The post-call summary for this session is not ready yet",
                          "details": {"session_id": "a1f3c9"}},
                "meta": null}""",
        )

        assertEquals(ApiError.SESSION_SUMMARY_PENDING, failureOf(api.getSessionSummary("a1f3c9")).code)
    }

    @Test
    fun `a 409 SESSION_ALREADY_ENDED maps onto its taxonomy member`() = runBlocking {
        enqueueError(
            409,
            """{"success": false, "data": null,
                "error": {"code": "SESSION_ALREADY_ENDED", "message": "This session has already ended",
                          "details": {"session_id": "a1f3c9"}},
                "meta": null}""",
        )

        assertEquals(ApiError.SESSION_ALREADY_ENDED, failureOf(api.remintSessionToken("a1f3c9")).code)
    }

    @Test
    fun `a 429 RATE_LIMITED maps onto its taxonomy member`() = runBlocking {
        enqueueError(
            429,
            """{"success": false, "data": null,
                "error": {"code": "RATE_LIMITED", "message": "Too many session creates"},
                "meta": null}""",
        )

        assertEquals(ApiError.RATE_LIMITED, failureOf(api.createSession(PHASE3_REQUEST)).code)
    }

    // -- docs/13 §9 forward compatibility -------------------------------------------

    @Test
    fun `an error code added next quarter folds to UNKNOWN but keeps the server's message`() = runBlocking {
        enqueueError(
            402,
            """{"success": false, "data": null,
                "error": {"code": "WALLET_FROZEN_PENDING_KYC",
                          "message": "Your wallet is frozen until KYC completes."},
                "meta": null}""",
        )

        val failure = failureOf(api.createSession(PHASE3_REQUEST))

        assertEquals(ApiError.UNKNOWN, failure.code)
        assertEquals("Your wallet is frozen until KYC completes.", failure.message)
    }

    // -- the body's verdict beats the status code (docs/13 §1) ----------------------

    @Test
    fun `a 200 whose body says success false with an error maps that error — the mangled-status rule`() = runBlocking {
        enqueueError(
            200,
            """{"success": false, "data": null,
                "error": {"code": "SESSION_CAPACITY", "message": "No voice-worker capacity"},
                "meta": null}""",
        )

        assertEquals(ApiError.SESSION_CAPACITY, failureOf(api.createSession(PHASE3_REQUEST)).code)
    }

    // -- malformed bodies fold, never throw -----------------------------------------

    @Test
    fun `a non-JSON error body folds to UNKNOWN with a showable message`() = runBlocking {
        enqueueError(502, "<html><body>Bad Gateway</body></html>")

        val failure = failureOf(api.createSession(PHASE3_REQUEST))

        assertEquals(ApiError.UNKNOWN, failure.code)
        assertTrue("message must be user-showable, not raw HTML", failure.message.isNotBlank())
        assertTrue("raw body must not leak into the message", !failure.message.contains("<html>"))
        assertNull(failure.details)
    }

    @Test
    fun `an error envelope with error null folds to UNKNOWN`() = runBlocking {
        enqueueError(500, """{"success": false, "data": null, "error": null, "meta": null}""")

        assertEquals(ApiError.UNKNOWN, failureOf(api.createSession(PHASE3_REQUEST)).code)
    }

    @Test
    fun `a 2xx with success true but data null folds to UNKNOWN`() = runBlocking {
        enqueueError(201, """{"success": true, "data": null, "error": null, "meta": null}""")

        assertEquals(ApiError.UNKNOWN, failureOf(api.createSession(PHASE3_REQUEST)).code)
    }

    @Test
    fun `truncated JSON on a 2xx folds to UNKNOWN instead of throwing`() = runBlocking {
        enqueueError(201, """{"success": true, "data": {"session_id": "a1f""")

        assertEquals(ApiError.UNKNOWN, failureOf(api.createSession(PHASE3_REQUEST)).code)
    }

    // -- transport failures fold, never throw ---------------------------------------

    @Test
    fun `a read timeout folds to an UNKNOWN failure instead of throwing`() = runBlocking {
        val impatient = apiAgainstServer(
            OkHttpClient.Builder().readTimeout(250, TimeUnit.MILLISECONDS).build(),
        )
        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.NO_RESPONSE))

        val failure = failureOf(impatient.createSession(PHASE3_REQUEST))

        assertEquals(ApiError.UNKNOWN, failure.code)
        assertTrue(failure.message.isNotBlank())
    }

    @Test
    fun `a connection dropped mid-handshake folds to an UNKNOWN failure`() = runBlocking {
        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_AT_START))

        assertEquals(ApiError.UNKNOWN, failureOf(api.createSession(PHASE3_REQUEST)).code)
    }

    private companion object {
        private val PHASE3_REQUEST = SessionCreateRequestDto(
            userId = "usr_rajesh01",
            screenContext = null,
            recentEvents = emptyList(),
        )
    }
}
