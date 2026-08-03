package com.vyaparpay.core.network.di

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
 * The long-lived transport singletons.
 *
 * `VyaparApi` and the repositories are not here yet — they arrive with the
 * envelope adapter (docs/03 §2.1). What is here is the pair every one of them
 * will share: one [OkHttpClient] so connection pooling is process-wide, and one
 * [Json] configured for the forward-compatibility rule in docs/13 §8.
 */
@Module
@InstallIn(SingletonComponent::class)
public object NetworkModule {

    @Provides
    @Singleton
    public fun provideJson(): Json = Json {
        // docs/13 §8: a server that adds a field must not break an older client.
        ignoreUnknownKeys = true
    }

    @Provides
    @Singleton
    public fun provideOkHttpClient(): OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(CONNECT_TIMEOUT_SECONDS, TimeUnit.SECONDS)
        .readTimeout(READ_TIMEOUT_SECONDS, TimeUnit.SECONDS)
        .build()
}
