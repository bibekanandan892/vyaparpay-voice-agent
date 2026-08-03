package com.vyaparpay.core.network

import kotlinx.serialization.json.Json
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import retrofit2.Retrofit
import retrofit2.converter.kotlinx.serialization.asConverterFactory

/**
 * Builds the [VyaparApi] Retrofit stack for a given base URL.
 *
 * The URL is a *parameter*, not a binding: no production URL is hardcoded
 * anywhere in this module, `:app` decides where the backend lives (and can
 * add its own `@Provides` on top of this factory once it owns that
 * decision), and JVM tests point it at a MockWebServer. Everything else —
 * the shared [OkHttpClient], the docs/13 §9-configured [Json], the
 * [EnvelopeAdapter] — is fixed here so every consumer gets the same stack.
 */
public class VyaparApiFactory(
    private val okHttpClient: OkHttpClient,
    private val json: Json,
) {

    /**
     * @param baseUrl the API root **including `/v1` and a trailing slash**,
     *   e.g. `https://api.example.test/v1/` (docs/13 §1: paths are relative
     *   to a base that already carries `/v1`). Must be `https` — docs/14
     *   mandates TLS on every wire — with one carve-out: cleartext to a
     *   loopback host, which never leaves the machine and is what lets JVM
     *   tests speak to a MockWebServer without a TLS fixture. On-device the
     *   manifest's `usesCleartextTraffic="false"` backstops this check.
     * @throws IllegalArgumentException on a malformed URL, a cleartext
     *   non-loopback URL, or a base URL without a trailing slash (fail fast
     *   at the boundary, not on the first call).
     */
    public fun create(baseUrl: String): VyaparApi {
        val url = baseUrl.toHttpUrl()
        require(url.isHttps || url.host in LOOPBACK_HOSTS) {
            "Base URL must be https (docs/14: TLS everywhere); cleartext is allowed only to loopback hosts for tests."
        }
        return Retrofit.Builder()
            .baseUrl(url)
            .client(okHttpClient)
            .addCallAdapterFactory(EnvelopeAdapter(json))
            .addConverterFactory(json.asConverterFactory(JSON_MEDIA_TYPE.toMediaType()))
            .build()
            .create(VyaparApi::class.java)
    }

    private companion object {
        /** docs/13 §1: JSON only, UTF-8. */
        private const val JSON_MEDIA_TYPE = "application/json; charset=utf-8"

        /** Hosts whose traffic never leaves the machine — the only permitted cleartext peers. */
        private val LOOPBACK_HOSTS = setOf("localhost", "127.0.0.1", "::1")
    }
}
