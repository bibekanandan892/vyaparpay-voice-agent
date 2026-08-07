package com.vyaparpay.core.network

import android.content.Context
import android.content.pm.PackageManager
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import okhttp3.Interceptor
import okhttp3.Response

/**
 * Demo-only bearer auth. `CallViewModel`'s own kdoc (`:feature:support`)
 * says it plainly: this app has no login screen, no token store, no `sub`
 * claim to read. The backend's `AuthMiddleware` still requires a signed
 * `Bearer` JWT on every route but `/health` (`backend/app/api/deps.py`'s
 * `verify_demo_jwt`), so without this, every request 403s regardless of
 * the base URL being correctly configured.
 *
 * Mirrors [com.vyaparpay.voice.service.VoiceCallService]'s own
 * `META_DATA_BASE_URL` channel rather than inventing a second configuration
 * mechanism: the host app supplies a token via `<meta-data>`, read here the
 * same way — off `<application>` rather than one component's manifest
 * entry, since the [okhttp3.OkHttpClient] this decorates is shared by every
 * `VyaparApi` call, not just the voice call's. Absent or blank means no
 * header is added — a build that never sets this meta-data behaves exactly
 * as it did before this interceptor existed.
 *
 * Not a real auth system: one static token for the process lifetime, no
 * refresh, no expiry handling beyond whatever the backend enforces
 * server-side. Fine for pointing a demo build at a seeded merchant; not
 * meant to survive past the day this app gets a real login flow.
 *
 * [tokenProvider] takes the primary constructor rather than [Context] —
 * same reasoning [ApiErrorReporter]'s own kdoc documents for its `clock`
 * parameter: Dagger/Hilt ignores Kotlin default parameter values entirely,
 * so a `Context`-only `@Inject` constructor would force every plain-JUnit
 * test in this module (none of them use Mockito/MockK or Robolectric — see
 * `ApiErrorReportingInterceptorTest` etc.) to fake an Android `Context`
 * just to construct an [okhttp3.OkHttpClient] that doesn't care about auth
 * at all. The narrower [Inject]-annotated secondary constructor below is
 * what Hilt actually resolves; tests use the primary constructor directly
 * with a trivial `{ null }`.
 */
public class DemoAuthInterceptor(
    private val tokenProvider: () -> String?,
) : Interceptor {

    /** Resolves the token from `<application>`'s meta-data — see this class's own kdoc. */
    @Inject
    public constructor(@ApplicationContext context: Context) : this(readManifestToken(context))

    override fun intercept(chain: Interceptor.Chain): Response {
        val request = chain.request()
        val token = tokenProvider()?.takeIf { it.isNotBlank() }
        val authorized = token?.let {
            request.newBuilder().header("Authorization", "Bearer $it").build()
        } ?: request
        return chain.proceed(authorized)
    }

    public companion object {
        /** `<meta-data>` key the host app sets on `<application>` to supply a demo JWT. */
        public const val META_DATA_DEMO_BEARER_TOKEN: String =
            "com.vyaparpay.core.network.DEMO_BEARER_TOKEN"

        /**
         * A plain (non-lambda-literal) companion function, not inlined into the
         * `@Inject` constructor's delegation call — a lambda literal there
         * tripped Kotlin's "Cannot access '<this>' before the instance has been
         * initialized" on this compiler, even though nothing inside it touches
         * an instance member.
         */
        private fun readManifestToken(context: Context): () -> String? = {
            runCatching {
                val info = context.packageManager.getApplicationInfo(
                    context.packageName,
                    PackageManager.GET_META_DATA,
                )
                info.metaData?.getString(META_DATA_DEMO_BEARER_TOKEN)
            }.getOrNull()
        }
    }
}
