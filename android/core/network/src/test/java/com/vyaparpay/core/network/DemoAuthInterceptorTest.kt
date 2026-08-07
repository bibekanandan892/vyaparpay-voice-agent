package com.vyaparpay.core.network

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertNull
import org.junit.Assert.assertEquals
import org.junit.Before
import org.junit.Test

/**
 * [DemoAuthInterceptor] in isolation, via a real [OkHttpClient] against
 * [MockWebServer] — the same style [ApiErrorReportingInterceptorTest] uses,
 * rather than hand-faking [okhttp3.Interceptor.Chain].
 */
class DemoAuthInterceptorTest {

    private lateinit var server: MockWebServer

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    private fun clientWith(tokenProvider: () -> String?): OkHttpClient =
        OkHttpClient.Builder().addInterceptor(DemoAuthInterceptor(tokenProvider)).build()

    private fun get(client: OkHttpClient) {
        server.enqueue(MockResponse().setResponseCode(200))
        client.newCall(Request.Builder().url(server.url("/v1/wallet")).build()).execute().close()
    }

    @Test
    fun `no token configured — no Authorization header is added`() {
        get(clientWith { null })

        assertNull(server.takeRequest().getHeader("Authorization"))
    }

    @Test
    fun `a blank token behaves the same as no token`() {
        get(clientWith { "   " })

        assertNull(server.takeRequest().getHeader("Authorization"))
    }

    @Test
    fun `a configured token is sent as a Bearer header`() {
        get(clientWith { "demo.jwt.token" })

        assertEquals("Bearer demo.jwt.token", server.takeRequest().getHeader("Authorization"))
    }

    @Test
    fun `the request body is untouched — this interceptor only adds a header`() {
        server.enqueue(MockResponse().setResponseCode(200))
        val client = clientWith { "demo.jwt.token" }
        val body = """{"user_id":"usr_rajesh01"}""".toRequestBody("application/json".toMediaType())

        client.newCall(Request.Builder().url(server.url("/v1/sessions")).post(body).build()).execute().close()

        val recorded = server.takeRequest()
        assertEquals("""{"user_id":"usr_rajesh01"}""", recorded.body.readUtf8())
        assertEquals("Bearer demo.jwt.token", recorded.getHeader("Authorization"))
    }
}
