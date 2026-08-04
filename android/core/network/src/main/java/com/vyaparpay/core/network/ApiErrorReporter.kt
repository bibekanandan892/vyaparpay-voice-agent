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
 */
public class ApiErrorReporter @Inject constructor(
    private val events: EventTracker,
    private val clock: () -> Long = System::currentTimeMillis,
) {

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
