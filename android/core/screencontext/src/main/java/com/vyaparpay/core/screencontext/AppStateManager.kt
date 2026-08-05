package com.vyaparpay.core.screencontext

import com.vyaparpay.core.analytics.AppEvent
import com.vyaparpay.core.analytics.EventTracker
import com.vyaparpay.core.network.RecentEventDto
import com.vyaparpay.core.network.SessionCreateRequestDto
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn

/**
 * Aggregates the three capture sources into one observable
 * `StateFlow<AppContextState>` — the single object the session-create
 * request builder and `ScreenContextPublisher` both read (docs/03 §3.11).
 *
 * **Judgment call 1 — the combine, redesigned against what actually got
 * built.** docs/03 §3.11's own sketch is
 * `combine(collector.ir, nav.flow, events.stream) { ir, flow, evs -> ... }`,
 * but neither side of that exists: `SemanticSnapshotBuilder` (T6) is a
 * stateless `object` with a pure `build(tree, screen, flow, recentEvents):
 * ScreenContextIr` function, not a class with a reactive `.ir` property —
 * the actual reactive source is `UiTreeCollector.tree: Flow<RawSemanticsTree>`.
 * The real, reviewed `EventTracker` interface is pull-only
 * (`record`/`recent`/`lastAction`); see [EventTracker.eventStream]'s own
 * kdoc for judgment call 2, which resolves that half separately for
 * `ScreenContextPublisher`'s benefit, not this class's.
 *
 * The combine actually implemented: `combine(rawSemanticsTree,
 * navigationTracker.route) { tree, route -> ... }`, calling
 * `SemanticSnapshotBuilder.build` synchronously inside the combine — pulling
 * `eventTracker.recent()` at that moment rather than subscribing to a push
 * stream, exactly as this task's brief anticipates. **Deliberately not a
 * three-way `combine` with `navigationTracker.flow` too**: `route` and `flow`
 * are two independent `MutableStateFlow`s that `NavigationTracker.
 * onDestinationChanged` updates sequentially, not atomically, so combining
 * both would let this class observe a transient `(newRoute, oldFlow)` state
 * between the two emissions. `flow` is a pure, `internal`-but-same-module
 * function of `route` (`NavigationTracker.flowFor`), so deriving it directly
 * inside the combine block is both race-free and definitionally identical to
 * what `navigationTracker.flow` would settle to a moment later.
 *
 * **Judgment call 1b — testability shape.** The class the docs sketch names
 * this constructor parameter `uiTreeCollector: UiTreeCollector`, but
 * `UiTreeCollectorTest`'s own kdoc establishes that `UiTreeCollector`'s walk
 * path (`attachRoot` -> real `RootForTest` -> `SemanticsNode`) "has no
 * meaningful fake on a plain JVM." A constructor typed at the whole
 * `UiTreeCollector` class would make [AppStateManagerTest] unable to drive
 * the tree side of the combine at all — there is no way to make a real
 * `UiTreeCollector.tree` emit a chosen fixture without a live Compose
 * composition. This class therefore has two constructors: an `internal`
 * primary one typed at `Flow<RawSemanticsTree>` (trivially fed a
 * `MutableStateFlow` in tests), and the `@Inject`-annotated secondary one
 * matching the doc's literal DI shape, which just supplies
 * `uiTreeCollector.tree`. Production/Hilt wiring is unaffected — a caller
 * outside this module still constructs this the way the doc describes.
 *
 * **Capture exclusion (docs/07 §2.1) lives here, not in
 * `ScreenContextPublisher`.** `sessionCreateBody` (below) must ship "the
 * retained last operational screen, not `HelpScreen`" (docs/13 §2.1), and
 * `sessionCreateBody` only ever reads `state.value.screen` — so the retention
 * has to already be baked into `state` by the time the publisher or the
 * session-create path reads it, not applied redundantly by each reader. This
 * class therefore never replaces its internally retained IR while the
 * current route is excluded (`HelpScreen`/`ConversationOverlay`/`flow ==
 * "support"`); `route`/`flow` on the emitted [AppContextState] still track
 * the *live* navigation position (useful to other readers, e.g. a future
 * `SupportButton` visibility check), but `screen` only ever advances on an
 * operational route. One consequence documented for `ScreenContextPublisher`
 * (see that class's kdoc): because `screen` structurally does not change
 * while the user is on an excluded route, the publisher's own diff-driven
 * "did anything change" check naturally emits nothing during that window —
 * capture exclusion does not need a second, duplicated implementation there.
 *
 * **Not resolved here: the `NavigationTracker` -> `UiTreeCollector.
 * forceImmediateCapture()` debounce-bypass wiring.** Both `NavigationTracker.
 * kt`'s and `UiTreeCollector.kt`'s kdocs point at "a future
 * NavigationTracker-driven publisher task" (i.e., plausibly this one) for
 * wiring navigation changes to bypass the 300 ms capture debounce (docs/07
 * §2.1). This class *could* subscribe to `navigationTracker.route` and call
 * `uiTreeCollector.forceImmediateCapture()` on each change, but doing so
 * would force the constructor back onto the concrete `UiTreeCollector` type
 * for that one side effect, reintroducing the exact testability problem
 * judgment call 1b resolves — for a purely latency optimization (a route
 * change is already reflected within one 300 ms debounce window, far below
 * the multi-second conversation-turn cadence docs/03 §5 itself cites as the
 * reason 300 ms is safe). Left as a documented, deliberate deferral rather
 * than a silent gap — a real follow-up if the bypass ever proves necessary,
 * not a correctness requirement for this task.
 *
 * @param scope the same "caller supplies a confined scope" convention
 *   `UiTreeCollector`/`VoiceCallCoordinator` already establish; `combine`
 *   itself runs on whatever dispatcher `scope` is backed by, not necessarily
 *   the UI thread — `.value` reads are safe from any thread regardless
 *   (`StateFlow`'s own guarantee).
 */
@Singleton
public class AppStateManager internal constructor(
    rawSemanticsTree: Flow<RawSemanticsTree>,
    private val navigationTracker: NavigationTracker,
    private val eventTracker: EventTracker,
    scope: CoroutineScope,
) {

    @Inject
    public constructor(
        uiTreeCollector: UiTreeCollector,
        navigationTracker: NavigationTracker,
        eventTracker: EventTracker,
        scope: CoroutineScope,
    ) : this(uiTreeCollector.tree, navigationTracker, eventTracker, scope)

    /**
     * Forwarded so `ScreenContextPublisher` — whose only dependency is this
     * class (docs/03 §3.10) — can subscribe to newly recorded events without
     * this module inventing a second way to reach `EventTracker`. See
     * [EventTracker.eventStream]'s kdoc for judgment call 2 in full, including
     * why that member is named `eventStream` rather than `events` (this
     * property is free to use the nicer name: `AppStateManager` is a brand
     * -new class with no pre-existing `events`-named fake anywhere to collide
     * with).
     */
    public val events: Flow<AppEvent> get() = eventTracker.eventStream

    // Mutated only from inside the single combine transform below, which
    // `combine` guarantees runs on one coroutine at a time — safe without a
    // lock, the same reasoning `VoiceCallCoordinator`'s own non-@Volatile
    // fields document (confinement, not visibility, is what makes this safe).
    private var retainedOperationalScreen: ScreenContextIr? = null

    /**
     * The last [RawSemanticsTree] this combine has already processed —
     * whether or not it produced a build (an excluded route observes a
     * capture and deliberately discards it, but the capture is still
     * *seen*). Distinct from [retainedOperationalScreen], which only ever
     * advances on a build; see [state]'s own comment for why both are
     * needed.
     */
    private var lastSeenTree: RawSemanticsTree? = null

    public val state: StateFlow<AppContextState> = combine(
        rawSemanticsTree,
        navigationTracker.route,
    ) { tree, route ->
        val flow = navigationTracker.flowFor(route)
        val recentEvents = eventTracker.recent()

        // **Audit fix (2026-08-04) — never build from a stale tree.**
        // `NavigationTracker.onDestinationChanged` sets `route` SYNCHRONOUSLY
        // inside the `NavController` destination-changed listener, which fires
        // during `navigate()` — before the destination's composable has
        // recomposed, and therefore long before `UiTreeCollector` has captured
        // it. A route change consequently wakes this combine while `tree` is
        // still the PREVIOUS screen's capture, and the old unconditional build
        // produced an IR labelled with the new route but populated with the old
        // screen's components. `ScreenContextPublisher` then saw
        // `previous.screen != newScreen.screen`, classified it as a screen
        // change, and shipped that fabrication as a full `ctx.snapshot` — the
        // agent reasoning about a screen the merchant was never on, on the one
        // signal this whole pipeline exists to get right.
        //
        // The guard: build only when a genuinely NEW capture arrived. The
        // reverse pairing (fresh tree, stale route) cannot occur — `route`
        // updates strictly before the recomposition a capture reflects — so
        // "a new tree arrived" is exactly the condition under which
        // `(tree, route)` is known-consistent.
        //
        // Identity (`!==`), not equality, is the right test for "a new capture
        // arrived": `UiTreeCollector.tree` is backed by a `MutableStateFlow`,
        // which already conflates structurally-equal values, so a re-emission
        // this combine can observe is always a distinct object.
        //
        // [lastSeenTree] advances on every capture INCLUDING excluded ones.
        // Skipping that update would reintroduce the same bug one step later:
        // leaving HelpScreen for an operational route would find the retained
        // tree pointer still on the pre-Help capture, mark Help's tree "new",
        // and build the new route's IR out of HelpScreen's components.
        //
        // No starvation risk: navigation always recomposes, so a capture always
        // follows within `UiTreeCollector`'s 300 ms debounce. Until it lands,
        // `screen` simply holds its previous, correctly-labelled value — the
        // same retained-IR shape capture exclusion already relies on, and one
        // `ScreenContextPublisher` handles by publishing nothing (its diff sees
        // no change), rather than by shipping something wrong.
        //
        // Review note (the trade-off this makes, stated plainly): if
        // `UiTreeCollector.start()`/`attachRoot()` were never wired, no capture
        // ever arrives and `screen` holds one stale-but-honestly-labelled IR
        // forever. Before this guard the same broken wiring produced loudly
        // WRONG content (a fresh route stamped onto old components) that QA
        // would spot immediately; now it produces quietly STALE content, which
        // is harder to notice. That is the deliberate trade — docs/08 §7's
        // "degrade, never lie" — but it means the capture wiring itself needs
        // its own liveness proof rather than relying on wrong output as a
        // smoke alarm. `MainActivityScreenContextTest` (`:app`) is that proof.
        val isNewCapture = tree !== lastSeenTree
        lastSeenTree = tree

        if (isNewCapture && !isExcludedFromCapture(route, flow)) {
            retainedOperationalScreen = SemanticSnapshotBuilder.build(
                tree = tree,
                screen = route,
                flow = flow,
                recentEvents = recentEvents,
            )
        }
        AppContextState(
            route = route,
            flow = flow,
            recentEvents = recentEvents,
            screen = retainedOperationalScreen,
        )
    }.stateIn(scope, SharingStarted.Eagerly, AppContextState())

    /**
     * The `POST /v1/sessions` body (docs/13 §2.1): `screenContext` is `null`
     * only when no operational screen has ever been captured (the retained
     * IR is itself unavailable — e.g. a cold start landing directly on
     * `HelpScreen`); `recentEvents` maps the real [AppEvent] sealed hierarchy
     * onto `protocol/schemas/app_event.v1.json`'s full per-variant shape (see
     * [toRecentEventDto]). [userId] is a plain passthrough — this class has
     * no identity of its own.
     *
     * **Audit fix (2026-08-05) — `asReversed()`, and why the reversal lives
     * HERE.** `session_create_request.v1.json` pins `recent_events` as
     * *oldest first* (and `protocol/fixtures/session_create_request.json` is
     * ts-ascending), but [EventTracker.recent] is newest-first by its own
     * documented contract — `RingBufferEventTracker` returns
     * `buffer.asReversed().take(count)`. This mapping used to ship that
     * order straight through, so the backend `RPUSH`ed the timeline
     * backwards into `ctx:{id}:events` and `ContextCompressor
     * .render_timeline_slot`'s `events[-15:]` "newest fifteen" slice would
     * have selected the OLDEST fifteen and rendered them in reverse — the
     * agent opening the call with the merchant's history read back to front.
     *
     * The reversal belongs at this one mapping, not in [EventTracker], and
     * not in [AppContextState]: newest-first is what every OTHER consumer of
     * that buffer wants and relies on — [EventTracker.lastAction] is
     * literally "the most recent entry", `SemanticSnapshotBuilder` scans the
     * same list front-to-back looking for the most recent `api_error` to
     * populate `last_api`, and `AppContextState.recentEvents` is documented
     * against the tracker's order. Flipping the shared contract to satisfy
     * one wire format would break all of them silently. `sessionCreateBody`
     * is the single place in the app that speaks `session_create_request/v1`,
     * so it is the single place the wire's ordering convention applies.
     *
     * `asReversed()` is a *view*, not a copy — the `.map` immediately after
     * it is what allocates, so this costs one list, not two.
     */
    public fun sessionCreateBody(userId: String): SessionCreateRequestDto {
        val current = state.value
        return SessionCreateRequestDto(
            userId = userId,
            screenContext = current.screen?.toJson(),
            recentEvents = current.recentEvents.asReversed().map { it.toRecentEventDto() },
        )
    }

    internal companion object {
        // Mirrors NavigationTracker's own ROUTE_TO_FLOW table's comment
        // (docs/07 §2.1): support surfaces are excluded from capture. This
        // set is intentionally NOT read off NavigationTracker (its table is
        // private) — docs/07 §2.1 names the excluded set directly, and this
        // is the one other place in the codebase that needs to test route
        // membership rather than look up a flow name.
        internal val EXCLUDED_ROUTES: Set<String> = setOf("HelpScreen", "ConversationOverlay")
        internal const val EXCLUDED_FLOW: String = "support"

        internal fun isExcludedFromCapture(route: String, flow: String): Boolean =
            route in EXCLUDED_ROUTES || flow == EXCLUDED_FLOW
    }
}

/**
 * `AppEvent` -> `RecentEventDto`, the full `protocol/schemas/app_event.v1
 * .json` shape: the `{type, name, ts}` canon every variant carries, plus
 * that variant's own required fields from docs/08 §2.1's per-type table.
 *
 * **Audit fix (2026-08-05).** This used to project away every per-variant
 * field, on the stated grounds that they were "a data-channel-only
 * concern". They are not: `app_event.v1.json` names session-create's
 * `recent_events` as one of the three places it is the shape authority for,
 * and `protocol/fixtures/session_create_request.json` carries the full
 * shape in all eight of its events. The projection cost the agent
 * `api_error`'s `status`/`code` — the one fact that says *which* failure
 * the merchant just hit, and the reason they are calling — and
 * `dialog.visible`.
 *
 * Deliberately the same mapping as `ScreenContextPublisher.kt`'s
 * `AppEvent.toWirePayload()`, which builds the identical five-variant shape
 * for the `ctx.event` data channel — one schema, two encoders. They stay
 * separate because they emit different types (a typed DTO here, a
 * `JsonObject` there, since the data-channel envelope's payload is
 * uninterpreted) and live in modules with different dependencies; that
 * publisher's kdoc already documents and accepts the same duplication in
 * the other direction. The exhaustive `when` over the sealed [AppEvent] is
 * what keeps them from drifting apart *silently*: adding a sixth event type
 * fails compilation in both files, which is exactly the closed-taxonomy
 * guarantee docs/08 §2.1 asks for ("a new event type is a protocol version
 * bump, not a config flag").
 *
 * The `type` string still comes from `type.name.lowercase()` — duplicating
 * `SemanticSnapshotBuilder.kt`'s private, file-scoped `AppEventType
 * .wireName()` rather than reusing it, since that extension is `private` to
 * its own file and `:core:analytics` exposes no public wire-name helper.
 * `API_ERROR.name.lowercase()` is `"api_error"`, matching the schema's enum
 * exactly.
 */
private fun AppEvent.toRecentEventDto(): RecentEventDto {
    val wireType = type.name.lowercase()
    return when (this) {
        is AppEvent.Nav -> RecentEventDto(type = wireType, name = name, ts = ts, from = from)
        is AppEvent.Tap -> RecentEventDto(type = wireType, name = name, ts = ts, screen = screen)
        // `value` stays null for sensitive field classes, and a null default
        // is *omitted* from the JSON rather than encoded (see RecentEventDto's
        // kdoc) — docs/08 §2.1's "omit the value rather than sending
        // [REDACTED]", which is why the schema makes it optional-not-nullable.
        is AppEvent.Input -> RecentEventDto(type = wireType, name = name, ts = ts, value = value)
        is AppEvent.ApiErrorEvent ->
            RecentEventDto(type = wireType, name = name, ts = ts, status = status, code = code)
        is AppEvent.Dialog ->
            RecentEventDto(type = wireType, name = name, ts = ts, visible = visible)
    }
}
