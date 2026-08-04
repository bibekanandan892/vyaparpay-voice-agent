package com.vyaparpay.core.network

import com.vyaparpay.core.analytics.AppEvent
import com.vyaparpay.core.analytics.EventTracker
import javax.inject.Inject

/**
 * Writes the `api_error` half of the timeline.
 *
 * This is why docs/03 §1 draws `:core:network -> :core:analytics`: a failing
 * call is a fact about the user's session that no view-tree walk can produce,
 * so the network layer has to put it on the timeline itself. The interceptor
 * that calls this is [ApiErrorReportingInterceptor], added to the shared
 * OkHttpClient in `di/NetworkModule.kt`.
 *
 * Called from the OkHttp dispatcher thread — [EventTracker] implementations
 * must tolerate that.
 *
 * @param clock defaults to the wall clock; overridable so tests can pin `ts`.
 *
 * **Bug found and fixed while wiring Phase-4 T8a's Hilt graph end to end.**
 * `@Inject` was on the two-arg constructor below with [clock] defaulted —
 * Dagger/Hilt ignores Kotlin default parameter values entirely (the same
 * limitation `UiTreeCollector`'s and every `@HiltViewModel`'s own `@Inject`
 * constructor kdoc documents in `:core:screencontext`/`:feature:*`), so
 * `:app:assembleDebug`'s Hilt aggregation step (`VyaparApp_HiltComponents`)
 * demanded a binding for the unqualified `kotlin.jvm.functions.Function0
 * <Long>` type `() -> Long` erases to — a binding nothing in this graph
 * provides or should. This was latent, not something this task's own
 * changes introduced: `:core:network` already applied the Hilt plugin, and
 * `NetworkModule.provideOkHttpClient` already declared
 * `ApiErrorReportingInterceptor` as a parameter, so this was broken the
 * moment `ApiErrorReportingInterceptor`/[ApiErrorReporter] first got their
 * `@Inject` constructors (Phase-4 T2) — it simply had never been caught by
 * a completed `:app:assembleDebug` run before now. Fixed the same way every
 * other instance of this pattern is fixed in this codebase: [clock] moved
 * off the `@Inject`-annotated constructor entirely, onto a narrower one
 * below that Dagger can actually satisfy.
 */
public class ApiErrorReporter(
    private val events: EventTracker,
    private val clock: () -> Long,
) {

    /** Delegates to [System.currentTimeMillis] -- see this class's own kdoc for why this, not the primary constructor, carries `@Inject`. */
    @Inject
    public constructor(events: EventTracker) : this(events, System::currentTimeMillis)

    /**
     * Records one failed call.
     *
     * The `app_event/v1` `api_error` variant's wire shape
     * (`protocol/schemas/app_event.v1.json`, docs/08 §2.1) is `{type, name,
     * ts, status, code}` — deliberately **no `screen`**, unlike `tap`. `nav`
     * and `tap` already carry the screen the user was on; an `api_error`
     * fires in reaction to one of those and sits right next to it in the
     * same 50-entry ring buffer, so a `screen` field here would be
     * redundant with the entry immediately before it, not new information.
     * That is also why this method takes no `screen` parameter: adding one
     * that never reaches the wire would be a dead parameter the schema
     * cannot use.
     *
     * @param error the mapped taxonomy code — never the raw response body,
     *   which may carry values the timeline must not retain. Rendered as the
     *   event's `code`.
     * @param method the HTTP method, e.g. `"POST"`.
     * @param path the short, `/v1`-stripped path, e.g. `"/sessions"` —
     *   combined with [method] into the event's `name` (docs/08 §2.1:
     *   `"METHOD path"`, e.g. `"POST /sessions"`).
     * @param status the HTTP status code, e.g. `402`.
     */
    public fun report(error: ApiError, method: String, path: String, status: Int) {
        events.record(
            AppEvent.ApiErrorEvent(
                name = "$method $path",
                ts = clock(),
                status = status,
                code = error.name,
            ),
        )
    }
}
