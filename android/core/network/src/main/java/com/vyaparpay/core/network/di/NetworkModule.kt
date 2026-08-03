package com.vyaparpay.core.network.di

import com.vyaparpay.core.network.VyaparApiFactory
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.components.SingletonComponent
import kotlinx.serialization.json.Json
import okhttp3.OkHttpClient
import java.util.concurrent.TimeUnit
import javax.inject.Singleton

private const val CONNECT_TIMEOUT_SECONDS = 10L
private const val READ_TIMEOUT_SECONDS = 30L

/**
 * The long-lived transport singletons (docs/03 §2.2: app-lifetime scope).
 *
 * One [OkHttpClient] so connection pooling is process-wide, one [Json]
 * configured for the forward-compatibility rule in docs/13 §9, and one
 * [VyaparApiFactory] that assembles the Retrofit stack around them.
 *
 * Deliberately *not* here: a `VyaparApi` binding. The base URL is an
 * `:app`-owned decision (build type, env) that this module must not hardcode,
 * so `:app` provides `VyaparApi` by calling [VyaparApiFactory.create] with
 * the URL it owns — keeping this module's graph fully satisfiable on its own.
 */
@Module
@InstallIn(SingletonComponent::class)
public object NetworkModule {

    @Provides
    @Singleton
    public fun provideJson(): Json = Json {
        // docs/13 §9: a server that adds a field must not break an older client.
        ignoreUnknownKeys = true
    }

    @Provides
    @Singleton
    public fun provideOkHttpClient(): OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(CONNECT_TIMEOUT_SECONDS, TimeUnit.SECONDS)
        .readTimeout(READ_TIMEOUT_SECONDS, TimeUnit.SECONDS)
        .build()

    @Provides
    @Singleton
    public fun provideVyaparApiFactory(
        okHttpClient: OkHttpClient,
        json: Json,
    ): VyaparApiFactory = VyaparApiFactory(okHttpClient, json)
}
