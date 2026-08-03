package com.vyaparpay.core.screencontext

import com.vyaparpay.core.analytics.AppEvent
import com.vyaparpay.core.network.ApiError

/**
 * The aggregated bundle both the session-create request builder and the in-call
 * publisher read (docs/03 §3.11) — the app-side analogue of the backend's
 * `ContextBuilder`: many inputs, one deterministic object.
 *
 * `screen` (the `screen_context/v1` IR from `SemanticSnapshotBuilder`) is
 * deliberately absent until the IR type lands in docs/07's transform; what is
 * here is the part that already has a home: the navigation position and the two
 * facts a view-tree walk cannot produce.
 */
public data class AppContextState(
    val route: String = "",
    val flow: String = "",
    val recentEvents: List<AppEvent> = emptyList(),
    val lastApiError: ApiError? = null,
) {
    /**
     * Whether this state is safe to ship.
     *
     * An empty route means nothing has been captured yet — publishing that would
     * tell the agent the user is nowhere, which is worse than telling it nothing.
     */
    public val isPublishable: Boolean get() = route.isNotEmpty()
}
