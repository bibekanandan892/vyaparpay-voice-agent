package com.vyaparpay.core.screencontext

import com.vyaparpay.core.analytics.AppEvent
import com.vyaparpay.core.network.ApiError

/**
 * The aggregated bundle both the session-create request builder and the in-call
 * publisher read (docs/03 §3.11) — the app-side analogue of the backend's
 * `ContextBuilder`: many inputs, one deterministic object.
 *
 * `screen` (the `screen_context/v1` IR from `SemanticSnapshotBuilder`) landed
 * with T6 (`ScreenContextIr`) and is wired here by `AppStateManager`
 * (Phase-4 T7b): the navigation position and the two facts a view-tree walk
 * cannot produce were "the part that already has a home" before this; now
 * every part does.
 *
 * **`screen` is the *retained operational* IR, not necessarily "whatever
 * `route` currently is."** `AppStateManager` never replaces `screen` while
 * `route`/`flow` are on an excluded capture surface (docs/07 §2.1: `HelpScreen`,
 * `ConversationOverlay`, `flow == "support"`) — so `route` can legitimately be
 * `"HelpScreen"` while `screen.screen` still reads `"PaymentScreen"`. That gap
 * is deliberate, not a bug: it is exactly what lets `sessionCreateBody` ship
 * the last operational screen the agent should reason about, per docs/13 §2.1's
 * canonical request. `screen` is `null` only when nothing operational has ever
 * been captured yet (e.g. a cold start landing directly on `HelpScreen`).
 *
 * `lastApiError` predates `screen` and has no wired producer of its own —
 * `AppStateManager`'s combine does not set it (see that class's kdoc): the
 * equivalent fact now lives in `screen?.lastApi`, sourced the same way
 * `SemanticSnapshotBuilder` already sources it (scanning `EventTracker`,
 * not a second, independently-mutated holder in `:core:network` — the same
 * "avoid two sources of truth for one fact" reasoning `SemanticSnapshotBuilder.kt`
 * documents for its own `lastApiFrom`). The field is left in place rather than
 * removed since removing it is outside this task's brief and nothing else in
 * this module reads it as a source of truth.
 */
public data class AppContextState(
    val route: String = "",
    val flow: String = "",
    val recentEvents: List<AppEvent> = emptyList(),
    val lastApiError: ApiError? = null,
    val screen: ScreenContextIr? = null,
) {
    /**
     * Whether this state is safe to ship.
     *
     * An empty route means nothing has been captured yet — publishing that would
     * tell the agent the user is nowhere, which is worse than telling it nothing.
     */
    public val isPublishable: Boolean get() = route.isNotEmpty()
}
