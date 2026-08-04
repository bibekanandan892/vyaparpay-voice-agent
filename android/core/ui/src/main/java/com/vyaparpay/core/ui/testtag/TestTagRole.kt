package com.vyaparpay.core.ui.testtag

/**
 * The IR roles a captured node can carry (docs/03 §4, docs/07 §4).
 *
 * [UNRESOLVED] is what a node gets when its testTag follows no convention —
 * the signal the `:core:screencontext` lint check turns into a warning.
 *
 * Named for documentation/completeness against `ScreenComponentRole`'s full
 * sixteen-member wire vocabulary (`:core:screencontext`'s `ScreenContextIr.kt`),
 * but only [IMAGE] has a testTag convention wired below in [TestTagRoles] and
 * `RoleMapper.fromTestTagRole()`. [TEXT_FIELD], [LIST], [LIST_ITEM], [DIALOG],
 * [TAB], and [TOGGLE] are resolved structurally/heuristically by `RoleMapper`
 * instead (see that class's kdoc for why) — their presence here does not mean
 * `roleFor` can produce them; `roleFor` never returns them.
 */
public enum class TestTagRole(public val wireName: String) {
    PRIMARY_CTA("primary_cta"),
    SECONDARY_CTA("secondary_cta"),
    AMOUNT_FIELD("amount_field"),
    RECIPIENT("recipient"),
    BALANCE_DISPLAY("balance_display"),
    STATUS_BADGE("status_badge"),
    SNACKBAR("snackbar"),
    ERROR_BANNER("error_banner"),
    ALERT_BANNER("alert_banner"),
    TEXT_FIELD("text_field"),
    LIST("list"),
    LIST_ITEM("list_item"),
    DIALOG("dialog"),
    TAB("tab"),
    TOGGLE("toggle"),
    IMAGE("image"),
    UNRESOLVED("unresolved"),
}

/**
 * The testTag naming convention that makes IR role assignment a lookup rather
 * than a guess (docs/03 §4).
 *
 * The tags exist because the app has Compose UI tests; screen-awareness rides
 * on that investment instead of paying for its own annotation pass. Keeping the
 * mapping here — in the module that also supplies `trackedClickable` — means the
 * convention is enforced at the point screens are written, not discovered later
 * by a snapshot that produced `unresolved` for half a screen.
 */
public object TestTagRoles {

    /**
     * Resolves a testTag to its IR role.
     *
     * The clauses are ordered: the first match wins, so a tag like
     * `recipient_amount` resolves as an amount field rather than a recipient —
     * what the node *is* beats what it is *about*.
     */
    public fun roleFor(testTag: String): TestTagRole = when {
        testTag.endsWith("_cta") -> TestTagRole.PRIMARY_CTA
        testTag.endsWith("_secondary") -> TestTagRole.SECONDARY_CTA
        testTag == "amount_input" || testTag.endsWith("_amount") -> TestTagRole.AMOUNT_FIELD
        testTag.startsWith("recipient_") || testTag.startsWith("vendor_") -> TestTagRole.RECIPIENT
        testTag.endsWith("_balance") -> TestTagRole.BALANCE_DISPLAY
        testTag.endsWith("_status") -> TestTagRole.STATUS_BADGE
        testTag.endsWith("_snackbar") -> TestTagRole.SNACKBAR
        testTag.endsWith("_error") -> TestTagRole.ERROR_BANNER
        testTag.endsWith("_alert") -> TestTagRole.ALERT_BANNER
        testTag.endsWith("_image") -> TestTagRole.IMAGE
        else -> TestTagRole.UNRESOLVED
    }
}
