package com.vyaparpay.core.screencontext

import com.vyaparpay.core.analytics.AppEvent
import javax.inject.Inject
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.launchIn
import kotlinx.coroutines.flow.onEach
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.add
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray

/**
 * Owns what leaves the device in-call (docs/03 §3.10): debounce/conflate,
 * snapshot-vs-delta diffing, one client-monotonic `seq` spanning
 * `ctx.snapshot`/`ctx.delta`/`ctx.event`, and handing encoded bytes to the
 * transport. [ContextChannel] is the port that keeps `:voice`/`org.webrtc`
 * out of this module (docs/03 §1) — this class never imports it.
 *
 * **Judgment call — testability shape (same reasoning as [AppStateManager]'s
 * own dual-constructor, see that class's kdoc).** The docs' sketch types this
 * constructor at the whole `AppStateManager`, but this class only ever reads
 * two things off it: [AppStateManager.state] and [AppStateManager.events].
 * An `AppStateManager` cannot be faked (concrete class, and its own tree
 * dependency chains back to the same plain-JVM wall [AppStateManager]'s kdoc
 * documents), so [ScreenContextPublisherTest] needs a seam narrower than the
 * whole class. The `internal` primary constructor below is typed directly at
 * `StateFlow<AppContextState>`/`Flow<AppEvent>` — trivially fed
 * `MutableStateFlow`/`MutableSharedFlow` in tests — with the `@Inject`
 * secondary constructor supplying `appState.state`/`appState.events` for
 * production/DI, matching the doc's literal shape from the outside.
 *
 * **Seq numbering and the genesis anchor.** `backend/app/context/
 * snapshot_ingestor.py`'s `_GENESIS_SEQ = 0` is the seq the server seeds for
 * the REST-ingested initial snapshot (`POST /v1/sessions`, which carries no
 * `seq` of its own on the wire) — the anchor an in-call `ctx.delta`'s first
 * `base_seq` would need to reconcile against, per that file's own judgment
 * call 4, flagged there as "a question for the on-device publisher spec...
 * not something this component can resolve unilaterally." Resolved here: this
 * publisher's own `seq` counter starts at `GENESIS_SEQ + 1` = 1, and — because
 * this class has no memory of "what was last published" until [start] begins
 * observing [AppStateManager.state] — the very first screen-state message it
 * ever sends is *always* a full `ctx.snapshot` (the "new screen" branch below
 * fires on a `null` `lastPublishedScreen`), matching docs/07 §6's own "screen
 * change -> always a full snapshot" rule applied to the bind-time transition
 * itself. This publisher therefore never attempts to diff *against* the
 * REST-ingested `seq = 0` state directly — it always re-establishes state
 * in-call with a fresh snapshot first, which is the same "fresh capture, not
 * a replay" principle docs/13 §3.3 uses for gap recovery, applied proactively
 * at bind time instead of reactively after a gap.
 *
 * **Capture exclusion is not re-checked here — see [AppStateManager]'s own
 * kdoc for why.** [AppContextState.screen] already structurally does not
 * change while the user is on an excluded route, so the "any change?" check
 * below naturally answers "no" and nothing is published — a second,
 * route-aware guard in this class would be dead code duplicating a decision
 * [AppStateManager] already made.
 *
 * **Where the token-budget drop ladder applies (docs/07 §7-8), and where it
 * does not.** [DropLadder.recompressOversize] runs on every `ctx.snapshot`
 * before it ships (§7: "before shipping a snapshot..., run the drop ladder").
 * It is deliberately **not** run on `ctx.delta` payloads: [DropLadder]'s own
 * rungs operate on the full IR shape (`ir["components"]` at the top level;
 * see `DropLadder.withComponents`), which a delta payload (`{base_seq,
 * changed, removed, ...}`) does not have — applying it directly would silently
 * no-op the component-shaped rungs and inject a spurious `components: []`
 * key into a shape that schema does not define. This reading is corroborated
 * from the other end of the wire: `snapshot_ingestor.py`'s own judgment call
 * 2 states "oversized deltas are dropped, not recompressed... the docs/07 §7
 * drop ladder is IR-shaped and cannot be safely applied to a delta's
 * changed/removed arrays without risking a corrupted merge" — the client and
 * server independently arrive at the same non-application. In practice this
 * is safe by construction here: the snapshot-vs-delta choice below already
 * compares the *raw* delta's estimated size against the *raw* full IR's, so
 * a delta large enough to need compression is, by that same comparison,
 * already no smaller than a snapshot — it is sent as a (droppable-ladder-
 * compressed) snapshot instead, never as an oversized delta.
 */
public class ScreenContextPublisher internal constructor(
    private val screenState: StateFlow<AppContextState>,
    private val events: Flow<AppEvent>,
    // Overridable in tests for a deterministic envelope `ts`, matching
    // NavigationTracker/ApiErrorReporter's own `clock: () -> Long` convention.
    private val clock: () -> Long = System::currentTimeMillis,
) {

    @Inject
    public constructor(appState: AppStateManager) : this(appState.state, appState.events)

    // One Mutex, not an AtomicLong, because a published message needs THREE
    // things updated together as a single unit — the next seq value, the
    // last-published IR, and the last-published-as-snapshot-or-delta seq
    // (base_seq anchor) — and screen-state publishes (conflated collector)
    // and event publishes (immediate collector) run as two independent
    // coroutines that can race each other for all three. Serializing the
    // whole "allocate seq, build bytes, send, update bookkeeping" unit is
    // also what keeps channel.send calls from interleaving mid-message.
    private val publishLock = Mutex()
    private var nextSeq: Long = GENESIS_SEQ + 1
    private var lastPublishedScreen: ScreenContextIr? = null

    // The seq a future delta's base_seq must point at — the last SNAPSHOT or
    // DELTA's seq, never an event's (docs/13 §4: events share the counter
    // but do not advance screen state).
    private var lastScreenStateSeq: Long? = null

    /**
     * Begins publishing. Two independent collectors on [scope], matching
     * docs/03 §5's split precisely: the screen-state side is conflated (a
     * slow channel drops intermediate `AppContextState`s rather than
     * queueing them) and events bypass both debounce and conflation
     * entirely, publishing the instant they land.
     *
     * **No explicit `.conflate()` call on [screenState].** `StateFlow` is
     * *already* a conflated hot flow by definition — a collector that falls
     * behind observes only the latest value, never a backlog (the
     * `StateFlow` docs' own "operator fusion" note); calling `.conflate()`
     * on one is a compile-time-deprecated no-op (Kotlin flags it as an
     * error, not just a warning, in the version this project pins), which is
     * how this was actually discovered — not a design choice made in
     * advance. `UiTreeCollector` still owns the 300 ms capture debounce
     * upstream (docs/03 §5); this class's "conflate" job is simply already
     * satisfied by [screenState]'s own type.
     */
    public fun start(channel: ContextChannel, scope: CoroutineScope) {
        screenState
            .onEach { state -> publishScreenState(state, channel) }
            .launchIn(scope)

        events
            .onEach { event -> publishEvent(event, channel) }
            .launchIn(scope)
    }

    private suspend fun publishScreenState(state: AppContextState, channel: ContextChannel) {
        val newScreen = state.screen ?: return
        publishLock.withLock {
            val previous = lastPublishedScreen

            if (previous == null || previous.screen != newScreen.screen) {
                sendSnapshot(newScreen, channel)
                return@withLock
            }
            if (previous == newScreen) return@withLock // nothing changed — publish nothing (docs/07 §6)

            val deltaPayload = buildDeltaPayload(previous, newScreen, baseSeq = lastScreenStateSeq ?: GENESIS_SEQ)
            val rawSnapshotPayload = newScreen.toJson()
            val deltaIsSmaller = DropLadder.estimateTokens(DropLadder.compactJson(deltaPayload)) <
                DropLadder.estimateTokens(DropLadder.compactJson(rawSnapshotPayload))

            if (deltaIsSmaller) {
                sendDelta(deltaPayload, channel)
                lastPublishedScreen = newScreen // the merged state advances even though the wire carried only the diff
            } else {
                sendSnapshot(newScreen, channel)
            }
        }
    }

    private suspend fun publishEvent(event: AppEvent, channel: ContextChannel) {
        publishLock.withLock {
            val seq = nextSeq++
            channel.send(encode(buildEnvelope(type = "ctx.event", seq = seq, payload = event.toWirePayload())))
            // lastScreenStateSeq is deliberately untouched: an event never
            // becomes a later delta's base_seq (docs/13 §4).
        }
    }

    /** Caller must already hold [publishLock]. */
    private suspend fun sendSnapshot(screen: ScreenContextIr, channel: ContextChannel) {
        val compressed = DropLadder.recompressOversize(screen.toJson()).ir
        val seq = nextSeq++
        channel.send(encode(buildEnvelope(type = "ctx.snapshot", seq = seq, payload = compressed)))
        lastPublishedScreen = screen
        lastScreenStateSeq = seq
    }

    /** Caller must already hold [publishLock]. */
    private suspend fun sendDelta(payload: JsonObject, channel: ContextChannel) {
        val seq = nextSeq++
        channel.send(encode(buildEnvelope(type = "ctx.delta", seq = seq, payload = payload)))
        lastScreenStateSeq = seq
    }

    private fun encode(envelope: JsonObject): ByteArray = DropLadder.compactJson(envelope).toByteArray(Charsets.UTF_8)

    private fun buildEnvelope(type: String, seq: Long, payload: JsonObject): JsonObject = buildJsonObject {
        put("v", 1)
        put("type", type)
        put("seq", seq)
        put("ts", clock())
        put("payload", payload)
    }

    /**
     * `ctx_delta.v1.json`'s payload (docs/07 §6): component identity for
     * diffing is **role+label** — testTag never reaches the wire (that
     * file's own description). [changed] carries each differing component
     * in full (not a field-level diff); [removed] carries identity pairs
     * only; the four optional top-level fields are present only when they
     * differ from [previous], omitted entirely otherwise (never `null` —
     * matching the schema's own `required: [base_seq, changed, removed]`
     * and nothing else).
     */
    private fun buildDeltaPayload(previous: ScreenContextIr, current: ScreenContextIr, baseSeq: Long): JsonObject {
        val previousByIdentity = previous.components.associateBy { it.role to it.label }
        val currentByIdentity = current.components.associateBy { it.role to it.label }

        val changed = current.components.filter { component ->
            previousByIdentity[component.role to component.label] != component
        }
        val removed = previous.components.filter { (it.role to it.label) !in currentByIdentity }

        return buildJsonObject {
            put("base_seq", baseSeq)
            putJsonArray("changed") { changed.forEach { add(it.toJson()) } }
            putJsonArray("removed") {
                removed.forEach { component ->
                    add(
                        buildJsonObject {
                            put("role", component.role.wireName)
                            put("label", component.label)
                        },
                    )
                }
            }
            if (current.lastAction != previous.lastAction) {
                // The delta schema's last_action, unlike the full IR's, has
                // no null variant — if the newest action somehow became
                // null (not observed in practice: EventTracker's ring
                // buffer only grows during a call), there is no
                // schema-legal way to represent "cleared" here, so the
                // field is left out and the previously-published value
                // stands. Same reasoning applies to last_api below.
                current.lastAction?.let { put("last_action", it.toDeltaJson()) }
            }
            if (current.lastApi != previous.lastApi) {
                current.lastApi?.let { put("last_api", it.toDeltaJson()) }
            }
            if (current.loading != previous.loading) put("loading", current.loading)
            if (current.dirtyFields != previous.dirtyFields) {
                putJsonArray("dirty_fields") { current.dirtyFields.forEach { add(it) } }
            }
        }
    }

    private companion object {
        // Same anchor as backend/app/context/snapshot_ingestor.py's
        // _GENESIS_SEQ — see this class's own kdoc for the full reasoning.
        private const val GENESIS_SEQ: Long = 0L
    }
}

/**
 * `ctx_event_payload`'s `{type, name, ts}` common shape, PLUS each variant's
 * own extra fields — `app_event.v1.json`'s full per-type shape, not just the
 * three fields the envelope schema itself pins as the minimum every variant
 * carries. The canonical docs/13 §4 example (`{"type":"tap","name":"Dismiss",
 * "screen":"PaymentScreen","ts":...}`) and `protocol/fixtures/context/
 * ctx_event_tap_dismiss.json` both carry `screen`, confirming the richer
 * shape is expected on the wire, not just tolerated — both schemas leave
 * `additionalProperties` unset for exactly this additive-within-v1 posture
 * (docs/13 §9).
 *
 * Duplicates the shape `SemanticSnapshotBuilder.kt`'s private
 * `AppEventType.wireName()` half-covers; see [AppStateManager.kt]'s
 * `toRecentEventDto` for the identical, separately-justified duplication.
 */
private fun AppEvent.toWirePayload(): JsonObject = buildJsonObject {
    put("type", type.name.lowercase())
    put("name", name)
    put("ts", ts)
    when (val self = this@toWirePayload) {
        is AppEvent.Nav -> put("from", self.from)
        is AppEvent.Tap -> put("screen", self.screen)
        is AppEvent.Input -> self.value?.let { put("value", it) }
        is AppEvent.ApiErrorEvent -> {
            put("status", self.status)
            put("code", self.code)
        }
        is AppEvent.Dialog -> put("visible", self.visible)
    }
}

/**
 * `ctx_delta.v1.json`'s locally-duplicated `last_action`/`last_api` `$defs`
 * — that schema's own file description explains the duplication ("this
 * walker only resolves local $ref... no schema file in this repo
 * cross-references another"). [ScreenContextIr.kt]'s equivalents
 * (`LastAction.toJson`/`LastApi.toJson`) are `private` to that file, so this
 * mirrors the schema family's own accepted duplication rather than reaching
 * for an inaccessible private declaration.
 */
private fun LastAction.toDeltaJson(): JsonObject = buildJsonObject {
    put("type", type)
    put("target", target)
    put("ts", ts)
}

private fun LastApi.toDeltaJson(): JsonObject = buildJsonObject {
    put("method", method)
    put("path", path)
    put("status", status)
    put("error_code", errorCode)
}
