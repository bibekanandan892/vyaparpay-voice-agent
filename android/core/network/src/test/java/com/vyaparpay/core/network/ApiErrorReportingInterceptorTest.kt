package com.vyaparpay.core.network

import com.vyaparpay.core.analytics.AppEvent
import com.vyaparpay.core.analytics.EventTracker
import com.vyaparpay.core.network.di.NetworkModule
import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * The interceptor half of the docs/08 §2.1 `api_error` contract: every
 * non-2xx [VyaparApi] response becomes exactly one timeline entry, mapped
 * with [ApiError]'s existing wire-code table rather than a second one.
 */
class ApiErrorReportingInterceptorTest {

    private class FakeEventTracker : EventTracker {
        val recorded = mutableListOf<AppEvent>()
        override fun record(event: AppEvent) {
            recorded += event
        }

        override fun recent(count: Int): List<AppEvent> = recorded.takeLast(count).asReversed()
        override val lastAction: AppEvent? get() = recorded.lastOrNull()
    }

    private lateinit var server: MockWebServer
    private lateinit var events: FakeEventTracker
    private lateinit var api: VyaparApi

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
        events = FakeEventTracker()
        val json = NetworkModule.provideJson()
        val interceptor = ApiErrorReportingInterceptor(ApiErrorReporter(events), json)
        val client = NetworkModule.provideOkHttpClient(interceptor)
        api = NetworkModule.provideVyaparApiFactory(client, json).create(server.url("/v1/").toString())
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    private val request = SessionCreateRequestDto(userId = "usr_rajesh01", screenContext = null, recentEvents = emptyList())

    @Test
    fun `a non-2xx response records an api_error event with METHOD, short path, status and code`() = runBlocking {
        server.enqueue(
            MockResponse().setResponseCode(402).setBody(
                """{"success": false, "data": null,
                    "error": {"code": "DAILY_LIMIT_EXCEEDED", "message": "Daily limit exceeded"},
                    "meta": null}""",
            ),
        )

        api.createSession(request)

        val event = events.recorded.single() as AppEvent.ApiErrorEvent
        assertEquals("POST /sessions", event.name)
        assertEquals(402, event.status)
        assertEquals("DAILY_LIMIT_EXCEEDED", event.code)
    }

    @Test
    fun `a successful response records nothing`() = runBlocking {
        server.enqueue(
            MockResponse().setResponseCode(201).setBody(
                """{"success": true, "data": {"session_id": "a1", "signaling_url": "wss://x",
                    "signaling_token": "st_x", "ice_servers": [], "expires": "2026-01-01T00:00:00Z"},
                    "error": null, "meta": null}""",
            ),
        )

        api.createSession(request)

        assertTrue(events.recorded.isEmpty())
    }

    @Test
    fun `an error code the client build does not recognise still records, folded to UNKNOWN`() = runBlocking {
        server.enqueue(
            MockResponse().setResponseCode(402).setBody(
                """{"success": false, "data": null,
                    "error": {"code": "WALLET_FROZEN_PENDING_KYC", "message": "frozen"},
                    "meta": null}""",
            ),
        )

        api.createSession(request)

        val event = events.recorded.single() as AppEvent.ApiErrorEvent
        assertEquals("UNKNOWN", event.code)
        assertEquals(402, event.status)
    }

    @Test
    fun `a non-JSON error body still records the status, folded to an UNKNOWN code`() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(502).setBody("<html><body>Bad Gateway</body></html>"))

        api.createSession(request)

        val event = events.recorded.single() as AppEvent.ApiErrorEvent
        assertEquals(502, event.status)
        assertEquals("UNKNOWN", event.code)
    }

    @Test
    fun `a DELETE to a path endpoint records its own method and short path`() = runBlocking {
        server.enqueue(
            MockResponse().setResponseCode(404).setBody(
                """{"success": false, "data": null,
                    "error": {"code": "SESSION_NOT_FOUND", "message": "not found"}, "meta": null}""",
            ),
        )

        api.endSession("zzz999")

        val event = events.recorded.single() as AppEvent.ApiErrorEvent
        assertEquals("DELETE /sessions/zzz999", event.name)
        assertEquals("SESSION_NOT_FOUND", event.code)
    }

    @Test
    fun `the response body remains readable downstream — peekBody does not consume it`() = runBlocking {
        server.enqueue(
            MockResponse().setResponseCode(402).setBody(
                """{"success": false, "data": null,
                    "error": {"code": "DAILY_LIMIT_EXCEEDED", "message": "Daily transaction limit exceeded",
                              "details": {"limit_paise": 2500000}},
                    "meta": null}""",
            ),
        )

        val result = api.createSession(request)

        check(result is ApiResult.Failure)
        assertEquals(ApiError.DAILY_LIMIT_EXCEEDED, result.code)
        assertTrue(result.message.startsWith("Daily transaction limit exceeded"))
        // And the interceptor still saw and recorded it independently.
        assertEquals(1, events.recorded.size)
    }
}
