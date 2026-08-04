package com.vyaparpay.core.network

import com.vyaparpay.core.analytics.RingBufferEventTracker
import com.vyaparpay.core.network.di.NetworkModule
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertThrows
import org.junit.Test

/**
 * The base-URL boundary: injectable, HTTPS-only (docs/14: TLS everywhere),
 * with cleartext permitted solely to loopback hosts so JVM tests can speak
 * to a MockWebServer.
 */
class VyaparApiFactoryTest {

    private val factory = NetworkModule.provideVyaparApiFactory(
        NetworkModule.provideOkHttpClient(
            ApiErrorReportingInterceptor(ApiErrorReporter(RingBufferEventTracker()), NetworkModule.provideJson()),
        ),
        NetworkModule.provideJson(),
    )

    @Test
    fun `an https base URL is accepted`() {
        assertNotNull(factory.create("https://api.example.test/v1/"))
    }

    @Test
    fun `a cleartext non-loopback base URL is rejected`() {
        assertThrows(IllegalArgumentException::class.java) {
            factory.create("http://api.example.test/v1/")
        }
    }

    @Test
    fun `cleartext to loopback is allowed — the MockWebServer seam`() {
        assertNotNull(factory.create("http://127.0.0.1:8080/v1/"))
        assertNotNull(factory.create("http://localhost:8080/v1/"))
    }

    @Test
    fun `a base URL that is not a URL at all fails fast`() {
        assertThrows(IllegalArgumentException::class.java) {
            factory.create("not a url")
        }
    }

    @Test
    fun `a base URL without a trailing slash fails fast rather than on the first call`() {
        assertThrows(IllegalArgumentException::class.java) {
            factory.create("https://api.example.test/v1")
        }
    }
}
