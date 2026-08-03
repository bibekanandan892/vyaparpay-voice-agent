package com.vyaparpay.core.network

import com.vyaparpay.core.analytics.AppEvent
import com.vyaparpay.core.analytics.AppEventType
import com.vyaparpay.core.analytics.EventTracker
import javax.inject.Inject

/**
 * Writes the `api_error` half of the timeline.
 *
 * This is why docs/03 §1 draws `:core:network -> :core:analytics`: a failing
 * call is a fact about the user's session that no view-tree walk can produce,
 * so the network layer has to put it on the timeline itself. The interceptor
 * that calls this lands with the Retrofit stack; the seam is here now so the
 * edge is load-bearing rather than decorative.
 *
 * Called from the OkHttp dispatcher thread — [EventTracker] implementations
 * must tolerate that.
 */
public class ApiErrorReporter @Inject constructor(
    private val events: EventTracker,
) {

    /**
     * Records one failed call.
     *
     * @param error the mapped taxonomy code — never the raw response body,
     *   which may carry values the timeline must not retain.
     * @param screen the route the call was made from.
     */
    public fun report(error: ApiError, screen: String) {
        events.record(
            AppEvent(
                type = AppEventType.API_ERROR,
                target = error.name,
                screen = screen,
            ),
        )
    }
}
