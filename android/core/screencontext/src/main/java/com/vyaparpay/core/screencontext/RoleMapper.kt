package com.vyaparpay.core.screencontext

import com.vyaparpay.core.ui.testtag.TestTagRole
import com.vyaparpay.core.ui.testtag.TestTagRoles

/**
 * The docs/07-ui-semantic-context.md §4 role vocabulary — the closed,
 * sixteen-member enum `protocol/schemas/screen_context.v1.json`'s `component`
 * `oneOf` freezes. [wireName] is the exact wire string.
 */
public enum class ScreenComponentRole(public val wireName: String) {
    AMOUNT_FIELD("amount_field"),
    RECIPIENT("recipient"),
    PRIMARY_CTA("primary_cta"),
    SECONDARY_CTA("secondary_cta"),
    TEXT_FIELD("text_field"),
    LIST("list"),
    LIST_ITEM("list_item"),
    DIALOG("dialog"),
    SNACKBAR("snackbar"),
    TAB("tab"),
    TOGGLE("toggle"),
    BALANCE_DISPLAY("balance_display"),
    STATUS_BADGE("status_badge"),
    ERROR_BANNER("error_banner"),
    ALERT_BANNER("alert_banner"),
    IMAGE("image"),
}

/**
 * docs/07 §5 rule 3 — "Map to roles": three tiers, first match wins.
 *
 * (a) **testTag convention**, resolved through [TestTagRoles.roleFor] — the
 * exact same function `:core:ui`'s own lint check and every already-merged
 * screen use, not a parallel/divergent mapping. [TestTagRole] names all
 * sixteen wire roles for documentation/completeness against
 * `ScreenComponentRole`, but only ten of them — the nine original members
 * plus `image` — actually resolve through [TestTagRoles.roleFor] and
 * [fromTestTagRole] below. The other six — `text_field`, `list`,
 * `list_item`, `dialog`, `tab`, `toggle` — have no testTag suffix
 * convention and [fromTestTagRole] maps them to `null`; they stay
 * structural/heuristic by design (dialog detection in particular is
 * explicitly *not* testTag-based — see `PaymentScreen.kt`'s own kdoc — so
 * [TestTagRole] having no testTag convention for `DIALOG` is correct, not a
 * gap).
 *
 * (b) **Semantics properties**, exact/structural: `IsDialog`, `Role.Tab`,
 * `ToggleableState`, `CollectionInfo`, `CollectionItemInfo`.
 *
 * (c) **Heuristics** — docs/07 §4's "else" column, implemented only for the
 * cases where a single node's own properties can decide *unambiguously*.
 * Five roles from that column are deliberately NOT given a node-local
 * heuristic here:
 *
 * `recipient` ("row with contact-shaped merged text adjacent to an amount
 * field") and `secondary_cta` ("outlined/text buttons adjacent to a
 * `primary_cta`") both require sibling *adjacency*, which is a property of
 * the merge pass ([SemanticsTreeMerger]), not of one node in isolation — by
 * the time a node reaches this resolver its neighbors are no longer visible
 * to it.
 *
 * `balance_display` and `status_badge` are left testTag/structural-only:
 * their heuristics ("non-editable currency text", "chip-shaped node with
 * enum-like text") would require treating *every* plain text leaf as a role
 * candidate, which would break rule 2's text-run-merge mechanism (a
 * currency-looking label would stop being mergeable into its owning field).
 *
 * `primary_cta` — "else the single filled `Role.Button` in the bottom
 * action area" — is the one deliberately DROPPED after being tried and
 * found unsafe during this task's own testing: `Role.Button` + `OnClick` is
 * not a distinguishing signal, it is the signal every clickable button
 * carries, including navigation chrome (docs/07 §1's own "Navigate up" back
 * button has exactly `Role.Button` + `ContentDescription` + `OnClick`, no
 * testTag). A heuristic of "any clickable Button" would resolve the back
 * button to `primary_cta` right alongside the real "Pay Now" CTA — a false
 * positive with real consequences (the agent would be told the screen has
 * two primary actions, or the wrong one). The doc's own qualifier — "the
 * *single* … button in the *bottom action area*" — is exactly the
 * positional/cardinality information this per-node resolver does not have.
 * `image` is dropped for the identical reason: `ContentDescription` +
 * `OnClick` with no `Role.Image` is indistinguishable from the same back
 * button. Both remain resolvable via tier (a) (testTag) only, which is also
 * the realistic path in this codebase: docs/03 §4's lint check already makes
 * testTag coverage for buttons under capture a build warning otherwise, so a
 * genuinely untagged CTA/image is the lint failure this heuristic tier would
 * otherwise be compensating for — better surfaced as that build warning than
 * guessed at silently and sometimes wrongly.
 */
internal object RoleMapper {

    /**
     * "Editable text matching a currency pattern" (docs/07 §4's `amount_field`
     * heuristic). Deliberately NOT an unbounded `[0-9,]+` pattern with an
     * optional currency symbol: a long run of digits with no symbol is
     * indistinguishable from a card number, phone number, or OTP — caught by
     * this task's own test suite, where a 16-digit card number originally
     * satisfied an earlier, looser version of this regex and mis-resolved to
     * `amount_field` instead of `text_field` (the value was still redacted
     * correctly either way, since redaction applies to both roles, but the
     * *role* was wrong, which the agent-facing IR should not be). This
     * version requires EITHER an explicit currency symbol prefix (any digit
     * length after it — "₹8,11,370" is a real amount), OR a bare digit/comma
     * run capped at 9 characters before any decimal point — generous enough
     * for an unformatted or Indian-grouped rupee amount ("245", "1,234.50",
     * "8,11,370") without a symbol, while a 13+ digit card-number-shaped run
     * (this project's own redaction backstop pattern, `CARD_NUMBER_LIKE` in
     * `SemanticSnapshotBuilder`, starts at 13 digits) is comfortably excluded.
     */
    private val CURRENCY_LIKE = Regex(
        "^[₹$€£¥]\\s?[0-9][0-9,]*(\\.[0-9]+)?$" + "|" + "^[0-9][0-9,]{0,8}(\\.[0-9]+)?$",
    )

    public fun resolve(node: RawSemanticsNode): ScreenComponentRole? {
        node.testTag?.let { tag ->
            fromTestTagRole(TestTagRoles.roleFor(tag))?.let { return it }
        }

        if (node.isDialog) return ScreenComponentRole.DIALOG
        if (node.role == RawRole.TAB) return ScreenComponentRole.TAB
        if (node.toggleableState != null) return ScreenComponentRole.TOGGLE
        if (node.collectionInfo != null) return ScreenComponentRole.LIST
        if (node.isListItem) return ScreenComponentRole.LIST_ITEM

        // Tier (c) heuristics — see class kdoc for what is deliberately absent.
        if (node.liveRegion == RawLiveRegion.POLITE) return ScreenComponentRole.SNACKBAR
        if (node.liveRegion == RawLiveRegion.ASSERTIVE) return ScreenComponentRole.ERROR_BANNER
        if (CURRENCY_LIKE.matches(node.editableText?.trim().orEmpty()) && node.editableText != null) {
            return ScreenComponentRole.AMOUNT_FIELD
        }
        if (node.editableText != null) return ScreenComponentRole.TEXT_FIELD
        // `primary_cta`/`image` heuristics deliberately absent — see class kdoc.
        return null
    }

    private fun fromTestTagRole(role: TestTagRole): ScreenComponentRole? = when (role) {
        TestTagRole.PRIMARY_CTA -> ScreenComponentRole.PRIMARY_CTA
        TestTagRole.SECONDARY_CTA -> ScreenComponentRole.SECONDARY_CTA
        TestTagRole.AMOUNT_FIELD -> ScreenComponentRole.AMOUNT_FIELD
        TestTagRole.RECIPIENT -> ScreenComponentRole.RECIPIENT
        TestTagRole.BALANCE_DISPLAY -> ScreenComponentRole.BALANCE_DISPLAY
        TestTagRole.STATUS_BADGE -> ScreenComponentRole.STATUS_BADGE
        TestTagRole.SNACKBAR -> ScreenComponentRole.SNACKBAR
        TestTagRole.ERROR_BANNER -> ScreenComponentRole.ERROR_BANNER
        TestTagRole.ALERT_BANNER -> ScreenComponentRole.ALERT_BANNER
        TestTagRole.IMAGE -> ScreenComponentRole.IMAGE
        TestTagRole.TEXT_FIELD,
        TestTagRole.LIST,
        TestTagRole.LIST_ITEM,
        TestTagRole.DIALOG,
        TestTagRole.TAB,
        TestTagRole.TOGGLE,
        TestTagRole.UNRESOLVED,
        -> null
    }
}

/**
 * A node is a candidate to become a component at all — i.e. it carries at
 * least one signal [RoleMapper.resolve] can act on. Nodes that are neither an
 * anchor candidate nor plain text (see [SemanticsTreeMerger]) contribute
 * nothing and are pruned by construction.
 */
internal fun isAnchorCandidate(node: RawSemanticsNode): Boolean =
    node.testTag != null ||
        node.isDialog ||
        node.role != null ||
        node.collectionInfo != null ||
        node.isListItem ||
        node.toggleableState != null ||
        node.hasOnClick ||
        node.liveRegion != null ||
        node.editableText != null
