package com.vyaparpay.core.screencontext.di

import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.components.SingletonComponent
import javax.inject.Singleton
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob

/**
 * Phase-4 T8a: the app-scoped [CoroutineScope] binding [UiTreeCollector] and
 * [AppStateManager] both take as an `@Inject` constructor parameter (neither
 * class is modified here — see each class's own kdoc; this module just makes
 * the one dependency they were already written against actually resolvable).
 *
 * **Judgment call 1 — unqualified, not a `@AppCoroutineScope` qualifier.**
 * Grepping every module's DI setup (`:core:network`'s [NetworkModule],
 * `:core:analytics`'s `AnalyticsModule`, `:voice`'s own hand-rolled scopes in
 * `VoiceCallService`/`VoiceCallCoordinator`) turns up zero existing
 * `CoroutineScope` binding anywhere in this graph — this is the first one.
 * `:voice`'s scopes (`VoiceCallService.serviceScope`,
 * `VoiceCallCoordinator`'s constructor-supplied `scope`) are deliberately
 * call-scoped, not app-scoped (docs/03 §3.3: a leaked `PeerConnection` is a
 * live mic, so that scope must die with the service, never survive as a
 * `@Singleton`) — they are a structurally different lifetime, not a second
 * consumer of *this* binding, and `:voice` cannot reach this module's
 * bindings anyway (docs/03 §1: `:voice -> :core:*` only, and this module
 * lives in `:core:screencontext`, a sideways edge `:voice` never takes).
 * With no second, differently-shaped consumer anywhere in the graph, adding a
 * qualifier now would be speculative generality (this codebase's own
 * `coding-style` convention: YAGNI) — an unqualified [CoroutineScope] is the
 * narrower, honest binding. If a second, genuinely different app-scoped
 * `CoroutineScope` need ever appears (e.g. one confined to `Dispatchers.IO`),
 * that is the moment to introduce a qualifier for both bindings at once, not
 * before.
 *
 * **Judgment call 2 — `Dispatchers.Main.immediate`, not `Dispatchers.Default`
 * or `Dispatchers.IO`.** [UiTreeCollector]'s own kdoc is explicit that its
 * `scope` "must be backed by a dispatcher confined to the UI thread" — every
 * `SemanticsNode` read only being valid from the thread that owns the
 * composition (docs/07 §2.1). [AppStateManager]'s `combine` has no such
 * requirement of its own (its kdoc: "runs on whatever dispatcher `scope` is
 * backed by, not necessarily the UI thread"), so the stricter of the two
 * requirements governs a shared, unqualified binding — `Dispatchers.Main`
 * satisfies both. `.immediate` (not plain `Dispatchers.Main`) avoids an
 * unnecessary dispatch hop for work that is *already* running on the main
 * thread at the moment a coroutine is launched (e.g. [MainActivity]'s
 * `onCreate` calling [UiTreeCollector.attachRoot] before any suspension has
 * occurred) — the same optimization `Dispatchers.Main.immediate` exists for
 * throughout the Android coroutines ecosystem.
 *
 * **`SupervisorJob`, not a plain `Job`.** Matches `VoiceCallService.
 * serviceScope`'s own choice for the identical reason: one misbehaving
 * collector (a bug in a future consumer of this scope) must not silently
 * cancel every other coroutine sharing this app-lifetime scope — a single
 * child's failure should not take down [UiTreeCollector]'s capture loop
 * *and* [AppStateManager]'s combine *and* whatever else the graph grows to
 * share this scope later.
 *
 * **Why this binding needs `kotlinx-coroutines-android` on `:core:
 * screencontext`'s runtime classpath, not just `-core`.** `Dispatchers.Main`
 * is *declared* in `kotlinx-coroutines-core`, but resolving it at runtime
 * without the `-android` artifact's `AndroidDispatcherFactory` (loaded via
 * `ServiceLoader`) throws `IllegalStateException: Module with the Main
 * dispatcher is missing`. See this module's `build.gradle.kts` for the
 * dependency this provider function requires to actually work, not just
 * compile.
 */
@Module
@InstallIn(SingletonComponent::class)
public object ScreenContextModule {

    @Provides
    @Singleton
    public fun provideCoroutineScope(): CoroutineScope =
        CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
}
