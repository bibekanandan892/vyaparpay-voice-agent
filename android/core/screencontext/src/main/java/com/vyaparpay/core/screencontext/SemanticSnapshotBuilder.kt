package com.vyaparpay.core.screencontext

import com.vyaparpay.core.analytics.AppEvent
import com.vyaparpay.core.analytics.AppEventType

/**
 * The pure transform docs/07-ui-semantic-context.md §5 specifies: a
 * [RawSemanticsTree] in, a [ScreenContextIr] out, deterministic (same tree in,
 * same IR out) — the property that makes the golden test in
 * `SemanticSnapshotBuilderTest` meaningful at all. Framework-free: nothing in
 * this file imports `androidx.compose.*` or `android.*`, so it runs on a
 * plain JVM (docs/07 §2.1's "keep the TRANSFORM logic framework-independent"),
 * matching this codebase's established split between a policy class and its
 * Android-framework-touching collaborator (`VoiceCallCoordinator` /
 * `AndroidCallAudioSession` is the precedent this follows).
 *
 * Rules 1-3 (prune, merge text runs, map to roles) live in
 * [SemanticsTreeMerger] + [RoleMapper] since role resolution has to happen
 * *during* the merge (see [SemanticsTreeMerger]'s own kdoc for why). This
 * class picks up from there: rule 4 (extract state, including rule 6's
 * redact-at-source applied inline to every label/value as it is produced —
 * "before the IR exists anywhere off the caller's stack" is only true if
 * redaction happens in the same pass that creates the field, not a later
 * one), rule 5 (rank and cap), and rule 7 (attach `last_action`/`last_api`/
 * `flow`). Rule 8 (serialize + drop ladder) is [ScreenContextIr.toJson] plus
 * [DropLadder] — a separate concern from *building* the IR, so callers that
 * only need the typed [ScreenContextIr] (a future `AppStateManager`, say)
 * are not forced through JSON.
 */
public object SemanticSnapshotBuilder {

    private const val MAX_COMPONENTS = 20
    private const val MAX_FIELD_CHARS = 120

    // Control characters (Cc) and the common zero-width/bidi-override code
    // points — docs/07 §5 rule 6: "zero-width and control characters
    // stripped... doubles as injection-surface reduction."
    private val CONTROL_OR_ZERO_WIDTH = Regex("[\\p{Cc}\\u200B-\\u200F\\u202A-\\u202E\\uFEFF]")

    // Rule 6 backstop pattern-matching, independent of the isSensitive flag
    // (docs/07 §5 rule 6: "declared per-field via a semantics modifier, PLUS
    // pattern matching as backstop"). Deliberately conservative — false
    // positives here cost a redacted amount/status; false negatives leak PII,
    // the asymmetric-risk direction that justifies erring wide.
    private val CARD_NUMBER_LIKE = Regex("""(?:\d[ -]?){13,19}""")
    private val PAN_LIKE = Regex("""\b[A-Z]{5}[0-9]{4}[A-Z]\b""")
    private val AADHAAR_LIKE = Regex("""\b(?:\d[ -]?){12}\b""")

    private val CURRENCY_SYMBOL_ONLY = Regex("^[₹$€£¥]$")

    public fun build(
        tree: RawSemanticsTree,
        screen: String,
        flow: String,
        recentEvents: List<AppEvent>,
    ): ScreenContextIr {
        val anchors = tree.roots.flatMap { SemanticsTreeMerger.merge(it) } // rules 1-3
        val components = anchors.map(::extractState) // rule 4 (+ rule 6 inline)
        val ranked = rankAndCap(components) // rule 5
        return ScreenContextIr(
            screen = screen,
            flow = flow,
            components = ranked,
            lastAction = recentEventsNewestFirstToLastAction(recentEvents),
            lastApi = lastApiFrom(recentEvents),
            dirtyFields = dirtyFieldsOf(anchors),
            loading = tree.roots.any(::subtreeHasProgressIndicator),
        )
    }

    // -- Rule 4: extract state (+ rule 6: redact/normalize at source) -------

    private fun extractState(anchor: MergedAnchor): ScreenComponent = when (anchor.role) {
        ScreenComponentRole.AMOUNT_FIELD -> {
            val (label, value) = splitCurrencyLabelAndValue(anchor)
            ScreenComponent.AmountField(
                label = normalize(label),
                value = redactedOrNormalized(anchor, value),
                focused = anchor.node.focused,
            )
        }
        ScreenComponentRole.RECIPIENT -> ScreenComponent.Recipient(
            label = normalize(anchor.textParts.getOrNull(0).orEmpty()),
            value = normalize(anchor.textParts.getOrNull(1) ?: anchor.editableText.orEmpty()),
        )
        ScreenComponentRole.PRIMARY_CTA -> ScreenComponent.PrimaryCta(
            label = normalize(primaryLabel(anchor)),
            enabled = anchor.node.enabled,
        )
        ScreenComponentRole.SECONDARY_CTA -> ScreenComponent.SecondaryCta(
            label = normalize(primaryLabel(anchor)),
            enabled = anchor.node.enabled,
        )
        ScreenComponentRole.TEXT_FIELD -> ScreenComponent.TextField(
            label = normalize(anchor.textParts.joinToString(" ")),
            value = redactedOrNormalized(anchor, anchor.editableText.orEmpty()),
            focused = anchor.node.focused,
        )
        ScreenComponentRole.LIST -> ScreenComponent.ListComponent(
            label = normalize(primaryLabel(anchor)),
            visibleCount = 0, // filled in by rankAndCap's caller — see listVisibleCounts
            totalCount = anchor.node.collectionInfo?.totalCount ?: 0,
        )
        ScreenComponentRole.LIST_ITEM -> ScreenComponent.ListItem(
            label = normalize(anchor.textParts.getOrNull(0).orEmpty()),
            value = normalize(anchor.textParts.getOrNull(1) ?: anchor.editableText.orEmpty()),
        )
        ScreenComponentRole.DIALOG -> ScreenComponent.Dialog(
            label = normalize(anchor.node.paneTitle ?: anchor.textParts.firstOrNull().orEmpty()),
        )
        ScreenComponentRole.SNACKBAR -> ScreenComponent.Snackbar(label = normalize(primaryLabel(anchor)))
        ScreenComponentRole.TAB -> ScreenComponent.Tab(
            label = normalize(primaryLabel(anchor)),
            selected = anchor.node.selected ?: false,
        )
        ScreenComponentRole.TOGGLE -> ScreenComponent.Toggle(
            label = normalize(primaryLabel(anchor)),
            on = anchor.node.toggleableState == RawToggleableState.ON,
            enabled = anchor.node.enabled,
        )
        ScreenComponentRole.BALANCE_DISPLAY -> ScreenComponent.BalanceDisplay(
            label = normalize(anchor.textParts.getOrNull(0).orEmpty()),
            value = normalize(anchor.textParts.getOrNull(1) ?: anchor.editableText.orEmpty()),
        )
        ScreenComponentRole.STATUS_BADGE -> ScreenComponent.StatusBadge(
            label = normalize(anchor.textParts.getOrNull(0).orEmpty()),
            value = normalize(anchor.textParts.getOrNull(1) ?: anchor.editableText.orEmpty()),
        )
        ScreenComponentRole.ERROR_BANNER -> ScreenComponent.ErrorBanner(label = normalize(primaryLabel(anchor)))
        ScreenComponentRole.ALERT_BANNER -> ScreenComponent.AlertBanner(label = normalize(primaryLabel(anchor)))
        ScreenComponentRole.IMAGE -> ScreenComponent.Image(
            label = normalize(anchor.node.contentDescription.firstOrNull() ?: primaryLabel(anchor)),
        )
    }

    /** The single-text-source roles: first merged text part, or empty. */
    private fun primaryLabel(anchor: MergedAnchor): String = anchor.textParts.firstOrNull().orEmpty()

    /**
     * `amount_field`'s currency-prefix handling (docs/07 §5 rule 2's own
     * worked example: "₹" + "Amount" + editable "245" -> label "Amount",
     * value "₹245"). A bare currency SYMBOL among the merged text parts
     * (exactly "₹", not e.g. "₹245") is pulled out and prefixed onto the
     * editable value instead of joining the label — every other merged part
     * stays in the label. This is deliberately scoped to `amount_field`
     * alone, matching docs/07 §4's own currency-pattern heuristic being
     * specific to that one role rather than a generic rule-2 text-merge
     * behavior.
     */
    private fun splitCurrencyLabelAndValue(anchor: MergedAnchor): Pair<String, String> {
        val prefix = anchor.textParts.firstOrNull { CURRENCY_SYMBOL_ONLY.matches(it.trim()) }
        val label = anchor.textParts.filterNot { it == prefix }.joinToString(" ")
        val rawValue = anchor.editableText.orEmpty()
        val value = if (prefix != null) "${prefix.trim()}$rawValue" else rawValue
        return label to value
    }

    // -- Rule 6: redact at source --------------------------------------------

    private fun redactedOrNormalized(anchor: MergedAnchor, value: String): String {
        val normalized = normalize(value)
        return if (isSensitive(anchor, normalized)) REDACTED else normalized
    }

    private fun isSensitive(anchor: MergedAnchor, value: String): Boolean =
        anchor.node.isSensitive ||
            CARD_NUMBER_LIKE.containsMatchIn(value) ||
            PAN_LIKE.containsMatchIn(value) ||
            AADHAAR_LIKE.containsMatchIn(value)

    private fun normalize(text: String): String {
        val stripped = CONTROL_OR_ZERO_WIDTH.replace(text, "")
        return if (stripped.length > MAX_FIELD_CHARS) stripped.take(MAX_FIELD_CHARS) else stripped
    }

    // -- Rule 5: rank and cap -------------------------------------------------

    /**
     * docs/07 §5 rule 5's six tiers, used ONLY as a retention priority when
     * there are more than [MAX_COMPONENTS] — NOT as a general sort key.
     *
     * **Judgment call.** docs/07 §3's own canonical example orders its five
     * components `[amount_field, recipient, primary_cta, dialog, snackbar]`
     * — fields and the CTA *before* the dialog and snackbar, even though
     * dialog/error_banner (tier 1) and snackbar (tier 2) nominally outrank
     * every other tier. Re-sorting the array by tier on every build would
     * make this class's output diverge from that canonical fixture, which is
     * the ground truth the golden test in `SemanticSnapshotBuilderTest`
     * checks against. Rule 5's own text is "order components by
     * support-relevance, THEN truncate to 20," and its later clarifying note
     * — "the canonical example's ordering... reflects reading order within
     * rank ties" — only makes sense if "rank" governs *what survives a cap*,
     * not the array's presentation order. This function therefore does two
     * different things depending on whether capping is even needed: under
     * the cap (the common case, and the golden fixture's case), the
     * components list is returned untouched, in pure traversal order; over
     * the cap, rank decides which [MAX_COMPONENTS] survive, but survivors are
     * still returned in their *original* traversal-order positions, not
     * grouped by tier. `SemanticSnapshotBuilderTest` exercises the over-cap
     * path explicitly since the golden fixture alone cannot.
     */
    private fun rankAndCap(components: List<ScreenComponent>): List<ScreenComponent> {
        val withCounts = fillListVisibleCounts(components)
        if (withCounts.size <= MAX_COMPONENTS) return withCounts
        val indexed = withCounts.withIndex().toList()
        val survivingIndices = indexed
            .sortedWith(compareBy({ tierOf(it.value) }, { it.index }))
            .take(MAX_COMPONENTS)
            .map { it.index }
            .toSet()
        return indexed.filter { it.index in survivingIndices }.map { it.value }
    }

    /**
     * `list`'s `visible_count` is the count of `list_item`s that immediately
     * follow it in traversal order (schema convention: containment is
     * positional, not a parent-id field — see `screen_context.v1.json`'s own
     * `list_item_component` description). Computed here, before rank/cap or
     * the drop ladder touch anything, so it reflects what Compose actually
     * realized (a `LazyColumn` only composes on-screen items), not any
     * truncation this builder itself performs afterward.
     */
    private fun fillListVisibleCounts(components: List<ScreenComponent>): List<ScreenComponent> {
        val out = components.toMutableList()
        for (i in out.indices) {
            val list = out[i] as? ScreenComponent.ListComponent ?: continue
            var count = 0
            var j = i + 1
            while (j < out.size && out[j] is ScreenComponent.ListItem) {
                count++
                j++
            }
            out[i] = list.copy(visibleCount = count)
        }
        return out
    }

    private fun tierOf(component: ScreenComponent): Int = when {
        component is ScreenComponent.Dialog || component is ScreenComponent.ErrorBanner -> 1
        component is ScreenComponent.Snackbar -> 2
        isFocusedField(component) -> 3
        component is ScreenComponent.PrimaryCta -> 4
        component is ScreenComponent.SecondaryCta -> 5
        component is ScreenComponent.AmountField ||
            component is ScreenComponent.Recipient ||
            component is ScreenComponent.BalanceDisplay ||
            component is ScreenComponent.StatusBadge -> 6
        else -> 7
    }

    private fun isFocusedField(component: ScreenComponent): Boolean = when (component) {
        is ScreenComponent.AmountField -> component.focused
        is ScreenComponent.TextField -> component.focused
        else -> false
    }

    // -- Rule 4 (cont.): dirty_fields / loading ------------------------------

    /**
     * **Judgment call.** Compose semantics has no native "edited since last
     * commit" concept — there is no property that distinguishes "the user
     * changed this and hasn't submitted" from "this field simply holds a
     * value." `Focused` (actively being edited right now) is the closest
     * available proxy and the one used here: it is conservative (a field the
     * user has already left, e.g. right after tapping Pay Now, is correctly
     * NOT dirty — matching docs/07 §3's own canonical example, whose
     * `amount_input` carries `Focused: false` post-submit and whose IR
     * carries `dirty_fields: []`), and testable. A true "uncommitted edit"
     * signal would need to originate in the ViewModel layer (comparing
     * current vs. last-submitted value) and flow down through a semantics
     * property this task does not invent — left for a future task if the
     * distinction between "focused" and "edited-but-unfocused" ever matters
     * in practice.
     */
    private fun dirtyFieldsOf(anchors: List<MergedAnchor>): List<String> = anchors
        .filter { it.node.focused && (it.role == ScreenComponentRole.AMOUNT_FIELD || it.role == ScreenComponentRole.TEXT_FIELD) }
        .map { it.node.testTag ?: it.role.wireName }

    private fun subtreeHasProgressIndicator(node: RawSemanticsNode): Boolean =
        (node.isVisible && !node.isZeroSize) &&
            (node.hasProgressIndicator || node.children.any(::subtreeHasProgressIndicator))

    // -- Rule 7: attach context beyond the tree ------------------------------

    /**
     * `last_action`: the newest **user-initiated** entry of the timeline —
     * `nav`/`tap`/`input` — unscoped by screen (rule 7's own wording, "most
     * recent entry of EventTracker's ring buffer," carries no "for this
     * screen" qualifier, unlike `last_api` below). [recentEvents] is
     * expected newest-first, matching `EventTracker.recent()`'s documented
     * contract.
     *
     * **Judgment call: why this filters at all**, when rule 7's prose reads
     * as "most recent entry, full stop." docs/07 §3's canonical example
     * gives `last_action = {"type": "tap", "target": "Pay Now", ...}` for a
     * snapshot whose OWN `last_api` shows the 402 decline already landed —
     * meaning this snapshot was captured *after* the `api_error` (and the
     * `dialog`-shown event that follows it, per docs/08 §2.4's worked
     * timeline for this exact incident) were already in the ring buffer.
     * Under a literal "most recent entry, no filter" reading, `last_action`
     * for this snapshot would be the `dialog` event, not the tap — which
     * contradicts the canonical fixture. Filtering to `nav`/`tap`/`input`
     * (genuine user actions) and skipping `api_error`/`dialog` (system-
     * reactive events, already represented by `last_api` and the `dialog`
     * *component* itself) is the reading that reproduces the canonical
     * fixture and also the more useful one: an agent asking "what did the
     * user just do" wants the tap, not an echo of a fact already sitting in
     * two other fields.
     *
     * **Known discrepancy, not fixed here.** `target` is `event.name`
     * passed through unchanged. docs/07 §3's canonical example shows
     * `last_action.target: "Pay Now"` — a human-readable label — but the
     * already-merged `Modifier.trackedClickable` (`:core:ui`) records a tap
     * event's `name` as the node's **testTag** (`"pay_now_cta"`), not its
     * label (`TrackedClickable.kt`: `AppEvent.Tap(name = testTag, ...)`).
     * Reconciling that would mean changing `trackedClickable`'s already-
     * frozen behavior, which is out of this task's scope (T3/T4 territory).
     * This builder is therefore faithful to whatever `name` the event
     * actually carries; `SemanticSnapshotBuilderTest`'s golden fixture
     * supplies `"Pay Now"` directly to match the canonical JSON for
     * comparison purposes, not because that is what production emits today.
     */
    private fun recentEventsNewestFirstToLastAction(recentEvents: List<AppEvent>): LastAction? =
        recentEvents
            .firstOrNull { it is AppEvent.Nav || it is AppEvent.Tap || it is AppEvent.Input }
            ?.let { LastAction(type = it.type.wireName(), target = it.name, ts = it.ts) }

    /**
     * **Judgment call: `last_api` sourcing.** `:core:network`'s
     * `ApiErrorReportingInterceptor` only *writes* the `api_error` timeline
     * entry (via `ApiErrorReporter`) — nothing in the merged codebase exposes
     * "the most recent non-2xx response" as its own queryable property. Two
     * options: (a) add a new mutable "last error" seam to `:core:network`, or
     * (b) derive it from `EventTracker.recent()`, the same source
     * `last_action` already reads. (b) wins: `:core:screencontext` already
     * treats `EventTracker` as the single source of truth for timeline facts
     * (its own `build.gradle.kts` comment: "neither is derivable from a
     * view-tree walk... both appear in `AppContextState`'s public signature");
     * adding a second, independently-mutated "last error" holder in
     * `:core:network` would create two sources of truth for the same fact
     * with no defined reconciliation, which this codebase avoids everywhere
     * else (`NavigationTracker`, `EventTracker` itself).
     *
     * The one real subtlety this creates: `AppEvent.ApiErrorEvent` carries no
     * `screen` field by design (`AppEvent.kt`'s own kdoc: "an api_error fires
     * in reaction to one of [nav/tap] and sits right next to it... a screen
     * field here would be redundant"). So "the most recent non-2xx response
     * FOR THIS SCREEN" (rule 7's literal wording) cannot be a blind "newest
     * `ApiErrorEvent` in the buffer" scan — that would leak a stale error
     * from a screen the user has since navigated away from. This function
     * scans [recentEvents] newest-first and returns the first
     * [AppEvent.ApiErrorEvent] found, UNLESS a [AppEvent.Nav] entry is
     * encountered first — a more recent navigation means any earlier error
     * belongs to the *previous* screen, so the scan stops and reports no
     * error for the current one.
     */
    private fun lastApiFrom(recentEvents: List<AppEvent>): LastApi? {
        for (event in recentEvents) {
            when (event) {
                is AppEvent.ApiErrorEvent -> {
                    val (method, path) = splitMethodAndPath(event.name)
                    return LastApi(method = method, path = path, status = event.status, errorCode = event.code)
                }
                is AppEvent.Nav -> return null
                else -> continue
            }
        }
        return null
    }

    private fun splitMethodAndPath(name: String): Pair<String, String> {
        val parts = name.split(" ", limit = 2)
        return parts.getOrElse(0) { "" } to parts.getOrElse(1) { "" }
    }

    private fun AppEventType.wireName(): String = name.lowercase()

    private const val REDACTED = "[REDACTED]"
}
