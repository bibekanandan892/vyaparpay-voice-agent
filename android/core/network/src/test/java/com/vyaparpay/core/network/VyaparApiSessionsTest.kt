package com.vyaparpay.core.network

import com.vyaparpay.core.analytics.RingBufferEventTracker
import com.vyaparpay.core.network.di.NetworkModule
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * Round-trips the four docs/13 §2 session endpoints through the real stack —
 * the DI-provided [Json], the [EnvelopeAdapter], the kotlinx converter —
 * against a MockWebServer speaking the documented bodies.
 */
class VyaparApiSessionsTest {

    private lateinit var server: MockWebServer
    private lateinit var api: VyaparApi
    private val json: Json = NetworkModule.provideJson()

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
        val factory = NetworkModule.provideVyaparApiFactory(
            NetworkModule.provideOkHttpClient(
                ApiErrorReportingInterceptor(ApiErrorReporter(RingBufferEventTracker()), json),
            ),
            json,
        )
        api = factory.create(server.url("/v1/").toString())
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    // -- POST /v1/sessions: response ------------------------------------------------

    @Test
    fun `session create round-trips every connect-bundle field of the docs 13 §2_1 body`() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(201).setBody(CANONICAL_CREATE_RESPONSE))

        val bundle = api.createSession(PHASE3_CREATE_REQUEST).dataOrFail()

        assertEquals("a1f3c9", bundle.sessionId)
        assertEquals("wss://voice.vyapar.local/v1/signal", bundle.signalingUrl)
        assertEquals("st_hV4nQ9pXzL2mR8cT0kWyB6uJ", bundle.signalingToken)
        assertEquals("2026-07-24T14:19:22+05:30", bundle.expires)

        assertEquals(2, bundle.iceServers.size)
        val stun = bundle.iceServers[0]
        assertEquals(listOf("stun:turn.vyapar.local:3478"), stun.urls)
        assertNull("STUN entry must decode with no username", stun.username)
        assertNull("STUN entry must decode with no credential", stun.credential)

        val turn = bundle.iceServers[1]
        assertEquals(
            listOf("turn:turn.vyapar.local:3478?transport=udp", "turns:turn.vyapar.local:5349"),
            turn.urls,
        )
        assertEquals("1784537062:a1f3c9", turn.username)
        assertEquals("kWx0mB4vQ2nT8hZJc6yUq1RfLpE=", turn.credential)
    }

    // -- POST /v1/sessions: request -------------------------------------------------

    @Test
    fun `session create sends POST to sessions with exactly the three schema keys`() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(201).setBody(CANONICAL_CREATE_RESPONSE))

        api.createSession(PHASE3_CREATE_REQUEST)

        val recorded = server.takeRequest()
        assertEquals("POST", recorded.method)
        assertEquals("/v1/sessions", recorded.path)
        assertTrue(
            "Content-Type must be application/json (docs/13 §1)",
            recorded.getHeader("Content-Type").orEmpty().startsWith("application/json"),
        )

        val body = json.parseToJsonElement(recorded.body.readUtf8()).jsonObject
        // additionalProperties: false — the client must send the schema's keys and nothing else.
        assertEquals(setOf("user_id", "screen_context", "recent_events"), body.keys)
        assertEquals("usr_rajesh01", body.getValue("user_id").jsonPrimitive.content)
        assertEquals(JsonNull, body.getValue("screen_context"))
        assertEquals(0, body.getValue("recent_events").jsonArray.size)
    }

    @Test
    fun `every event variant serializes with exactly its app_event v1 field set`() = runBlocking {
        // The docs/13 §2.1 canonical request's own timeline, verbatim from
        // protocol/fixtures/session_create_request.json — one event per
        // variant, in the fixture's oldest-first order. Each assertion is the
        // schema's `required` list for that $def, and the KEY SET is the
        // assertion that matters: it catches both a dropped per-variant field
        // (the audit finding this test exists for) and a null leaking onto
        // the wire for a variant that does not carry that field.
        server.enqueue(MockResponse().setResponseCode(201).setBody(CANONICAL_CREATE_RESPONSE))

        api.createSession(
            SessionCreateRequestDto(
                userId = "usr_rajesh01",
                screenContext = null,
                recentEvents = listOf(
                    RecentEventDto(
                        type = "nav",
                        name = "PaymentScreen",
                        ts = 1_784_536_395_000L,
                        from = "DashboardScreen",
                    ),
                    RecentEventDto(
                        type = "input", name = "amount", ts = 1_784_536_417_000L, value = "₹245",
                    ),
                    RecentEventDto(
                        type = "tap",
                        name = "Pay Now",
                        ts = 1_784_536_440_000L,
                        screen = "PaymentScreen",
                    ),
                    RecentEventDto(
                        type = "api_error",
                        name = "POST /payments",
                        ts = 1_784_536_440_820L,
                        status = 402,
                        code = "DAILY_LIMIT_EXCEEDED",
                    ),
                    RecentEventDto(
                        type = "dialog",
                        name = "Daily Limit Exceeded",
                        ts = 1_784_536_440_900L,
                        visible = true,
                    ),
                ),
            ),
        )

        val body = json.parseToJsonElement(server.takeRequest().body.readUtf8()).jsonObject
        val events = body.getValue("recent_events").jsonArray.map { it.jsonObject }
        assertEquals(5, events.size)

        val nav = events[0]
        assertEquals(setOf("type", "name", "ts", "from"), nav.keys)
        assertEquals("nav", nav.getValue("type").jsonPrimitive.content)
        assertEquals("PaymentScreen", nav.getValue("name").jsonPrimitive.content)
        assertEquals(1_784_536_395_000L, nav.getValue("ts").jsonPrimitive.long)
        assertEquals("DashboardScreen", nav.getValue("from").jsonPrimitive.content)

        val input = events[1]
        assertEquals(setOf("type", "name", "ts", "value"), input.keys)
        assertEquals("₹245", input.getValue("value").jsonPrimitive.content)

        val tap = events[2]
        assertEquals(setOf("type", "name", "ts", "screen"), tap.keys)
        assertEquals("PaymentScreen", tap.getValue("screen").jsonPrimitive.content)

        // The highest-value diagnostic in the whole timeline: which failure
        // the merchant just hit. The old three-field projection dropped both.
        val apiError = events[3]
        assertEquals(setOf("type", "name", "ts", "status", "code"), apiError.keys)
        assertEquals(402, apiError.getValue("status").jsonPrimitive.int)
        assertEquals("DAILY_LIMIT_EXCEEDED", apiError.getValue("code").jsonPrimitive.content)

        val dialog = events[4]
        assertEquals(setOf("type", "name", "ts", "visible"), dialog.keys)
        assertTrue(dialog.getValue("visible").jsonPrimitive.boolean)
    }

    @Test
    fun `an input event omits value entirely rather than sending null`() = runBlocking {
        // docs/08 §2.1: a sensitive field class makes the event "omit the
        // value rather than sending [REDACTED]" — which app_event.v1.json
        // states is "why value is optional here rather than required-and-
        // nullable". This is the assertion that pins kotlinx's
        // `encodeDefaults = false` (NetworkModule.provideJson) as part of the
        // wire contract rather than an incidental default.
        server.enqueue(MockResponse().setResponseCode(201).setBody(CANONICAL_CREATE_RESPONSE))

        api.createSession(
            SessionCreateRequestDto(
                userId = "usr_rajesh01",
                screenContext = null,
                recentEvents = listOf(
                    RecentEventDto(type = "input", name = "upi_pin", ts = 1_784_536_417_000L),
                ),
            ),
        )

        val body = json.parseToJsonElement(server.takeRequest().body.readUtf8()).jsonObject
        val event = body.getValue("recent_events").jsonArray[0].jsonObject
        assertEquals(setOf("type", "name", "ts"), event.keys)
    }

    @Test
    fun `recent events keep the order they were given, oldest first`() = runBlocking {
        // session_create_request.v1.json pins oldest-first, the backend
        // RPUSHes in array order, and ContextCompressor.render_timeline_slot
        // slices `events[-15:]` for "the newest 15" — so the transport must
        // not reorder. (The producer-side reversal of EventTracker's
        // newest-first buffer is AppStateManagerTest's subject.)
        server.enqueue(MockResponse().setResponseCode(201).setBody(CANONICAL_CREATE_RESPONSE))

        api.createSession(
            SessionCreateRequestDto(
                userId = "usr_rajesh01",
                screenContext = null,
                recentEvents = (0..4).map {
                    RecentEventDto(
                        type = "tap",
                        name = "e$it",
                        ts = 1_784_536_440_000L + it * 1_000L,
                        screen = "PaymentScreen",
                    )
                },
            ),
        )

        val body = json.parseToJsonElement(server.takeRequest().body.readUtf8()).jsonObject
        val names = body.getValue("recent_events").jsonArray
            .map { it.jsonObject.getValue("name").jsonPrimitive.content }
        assertEquals(listOf("e0", "e1", "e2", "e3", "e4"), names)
    }

    // -- docs/13 §9: unknown fields must be ignored ---------------------------------

    @Test
    fun `unknown extra fields at every nesting level are ignored, known fields survive`() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(201).setBody(CREATE_RESPONSE_WITH_EXTRAS))

        val bundle = api.createSession(PHASE3_CREATE_REQUEST).dataOrFail()

        assertEquals("a1f3c9", bundle.sessionId)
        assertEquals("st_hV4nQ9pXzL2mR8cT0kWyB6uJ", bundle.signalingToken)
        assertEquals(2, bundle.iceServers.size)
        assertEquals("1784537062:a1f3c9", bundle.iceServers[1].username)
    }

    // -- POST /v1/sessions/{id}/token -----------------------------------------------

    @Test
    fun `token re-mint posts to the session token path and returns a fresh bundle`() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(200).setBody(CANONICAL_CREATE_RESPONSE))

        val bundle = api.remintSessionToken("a1f3c9").dataOrFail()

        val recorded = server.takeRequest()
        assertEquals("POST", recorded.method)
        assertEquals("/v1/sessions/a1f3c9/token", recorded.path)
        assertEquals("a1f3c9", bundle.sessionId)
        assertEquals("st_hV4nQ9pXzL2mR8cT0kWyB6uJ", bundle.signalingToken)
    }

    // -- DELETE /v1/sessions/{id} ---------------------------------------------------

    @Test
    fun `hang-up sends DELETE and unwraps the terminal-state body`() = runBlocking {
        server.enqueue(
            MockResponse().setResponseCode(200).setBody(
                """{"success": true, "data": {"session_id": "a1f3c9", "state": "ended"},
                    "error": null, "meta": null}""",
            ),
        )

        val end = api.endSession("a1f3c9").dataOrFail()

        val recorded = server.takeRequest()
        assertEquals("DELETE", recorded.method)
        assertEquals("/v1/sessions/a1f3c9", recorded.path)
        assertEquals("a1f3c9", end.sessionId)
        assertEquals("ended", end.state)
    }

    // -- GET /v1/sessions/{id}/summary ----------------------------------------------

    @Test
    fun `summary round-trips the full docs 13 §2_3 body including resolution and actions`() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(200).setBody(CANONICAL_SUMMARY_RESPONSE))

        val summary = api.getSessionSummary("a1f3c9").dataOrFail()

        assertEquals("GET", server.takeRequest().method)
        assertEquals("a1f3c9", summary.sessionId)
        assertEquals("2026-07-24T14:14:24+05:30", summary.startedAt)
        assertEquals(316, summary.durationSeconds)
        assertEquals(15, summary.turnCount)
        assertTrue(summary.summary.contains("limit increase"))

        val resolution = summary.resolution
        assertEquals("limit_increase_requested", resolution?.type)
        assertEquals("LMT-2026-0724-0913", resolution?.reference)
        assertEquals(4, resolution?.etaHours)

        assertEquals(
            listOf(
                SessionActionDto(tool = "get_payment_status", status = "ok"),
                SessionActionDto(tool = "get_wallet_balance", status = "ok"),
                SessionActionDto(tool = "request_limit_increase", status = "ok"),
            ),
            summary.actions,
        )
    }

    @Test
    fun `summary with a null resolution decodes to null so clients can branch on it`() = runBlocking {
        server.enqueue(
            MockResponse().setResponseCode(200).setBody(
                """{"success": true, "data": {
                     "session_id": "a1f3c9", "started_at": "2026-07-24T14:14:24+05:30",
                     "duration_s": 42, "turn_count": 3, "summary": "Call lasted 42s over 3 turns.",
                     "resolution": null, "actions": []},
                   "error": null, "meta": null}""",
            ),
        )

        val summary = api.getSessionSummary("a1f3c9").dataOrFail()

        assertEquals("/v1/sessions/a1f3c9/summary", server.takeRequest().path)
        assertNull(summary.resolution)
        assertEquals(emptyList<SessionActionDto>(), summary.actions)
    }

    private companion object {
        /** What every Phase-3 client sends (protocol/schemas/session_create_request.v1.json). */
        private val PHASE3_CREATE_REQUEST = SessionCreateRequestDto(
            userId = "usr_rajesh01",
            screenContext = null,
            recentEvents = emptyList(),
        )

        /** The docs/13 §2.1 `201` body, verbatim. */
        private val CANONICAL_CREATE_RESPONSE = """
            {
              "success": true,
              "data": {
                "session_id": "a1f3c9",
                "signaling_url": "wss://voice.vyapar.local/v1/signal",
                "signaling_token": "st_hV4nQ9pXzL2mR8cT0kWyB6uJ",
                "ice_servers": [
                  {"urls": ["stun:turn.vyapar.local:3478"]},
                  {"urls": ["turn:turn.vyapar.local:3478?transport=udp",
                            "turns:turn.vyapar.local:5349"],
                   "username": "1784537062:a1f3c9",
                   "credential": "kWx0mB4vQ2nT8hZJc6yUq1RfLpE="}
                ],
                "expires": "2026-07-24T14:19:22+05:30"
              },
              "error": null,
              "meta": null
            }
        """.trimIndent()

        /** The same body with additive fields a future server might grow (docs/13 §9). */
        private val CREATE_RESPONSE_WITH_EXTRAS = """
            {
              "success": true,
              "server_build": "2027.01.1",
              "data": {
                "session_id": "a1f3c9",
                "signaling_url": "wss://voice.vyapar.local/v1/signal",
                "signaling_token": "st_hV4nQ9pXzL2mR8cT0kWyB6uJ",
                "relay_region": "in-south-1",
                "ice_servers": [
                  {"urls": ["stun:turn.vyapar.local:3478"], "ttl_s": 600},
                  {"urls": ["turn:turn.vyapar.local:3478?transport=udp",
                            "turns:turn.vyapar.local:5349"],
                   "username": "1784537062:a1f3c9",
                   "credential": "kWx0mB4vQ2nT8hZJc6yUq1RfLpE=",
                   "priority": 2}
                ],
                "expires": "2026-07-24T14:19:22+05:30"
              },
              "error": null,
              "meta": {"deprecation": null}
            }
        """.trimIndent()

        /** The docs/13 §2.3 summary body, verbatim. */
        private val CANONICAL_SUMMARY_RESPONSE = """
            {
              "success": true,
              "data": {
                "session_id": "a1f3c9",
                "started_at": "2026-07-24T14:14:24+05:30",
                "duration_s": 316,
                "turn_count": 15,
                "summary": "₹245 vendor payment to Amazon Business declined: daily limit exceeded (₹24,890 of ₹25,000 used). Submitted a limit increase to ₹50,000, reference LMT-2026-0724-0913, ETA 4 hours.",
                "resolution": {
                  "type": "limit_increase_requested",
                  "reference": "LMT-2026-0724-0913",
                  "eta_hours": 4
                },
                "actions": [
                  {"tool": "get_payment_status", "status": "ok"},
                  {"tool": "get_wallet_balance", "status": "ok"},
                  {"tool": "request_limit_increase", "status": "ok"}
                ]
              },
              "error": null,
              "meta": null
            }
        """.trimIndent()
    }
}

/** Asserts the result is a [ApiResult.Success] and returns its data. */
internal fun <T> ApiResult<T>.dataOrFail(): T {
    if (this !is ApiResult.Success) {
        throw AssertionError("Expected ApiResult.Success but was $this")
    }
    return data
}
