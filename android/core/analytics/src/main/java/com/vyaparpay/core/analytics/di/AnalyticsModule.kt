package com.vyaparpay.core.analytics.di

import com.vyaparpay.core.analytics.EventTracker
import com.vyaparpay.core.analytics.RingBufferEventTracker
import dagger.Binds
import dagger.Module
import dagger.hilt.InstallIn
import dagger.hilt.components.SingletonComponent
import javax.inject.Singleton

/**
 * Binds the [EventTracker] contract to its real, `@Singleton`
 * [RingBufferEventTracker] implementation.
 *
 * This module is what makes `:core:network`'s `ApiErrorReportingInterceptor`
 * (which needs an [EventTracker] transitively, via `ApiErrorReporter`)
 * resolvable once Hilt aggregates the whole graph at `:app`'s
 * `assembleDebug` — without it, that resolution fails at compile time, not
 * just at runtime, which is why this binding exists now rather than waiting
 * for a screen to actually inject one.
 */
@Module
@InstallIn(SingletonComponent::class)
public abstract class AnalyticsModule {

    @Binds
    @Singleton
    public abstract fun bindEventTracker(impl: RingBufferEventTracker): EventTracker
}
