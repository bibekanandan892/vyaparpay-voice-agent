package com.vyaparpay.feature.support

/**
 * The support hub — it hosts the `SupportButton` and the text-chat fallback
 * (docs/03 §3.6, §4).
 *
 * The one destination in the app that is **not** captured. Support surfaces are
 * excluded from the view-tree walk (docs/07 §2.1): the agent needs the screen
 * the merchant was stuck on, not the screen they went to for help. The same
 * rule is why the `SupportButton` hides itself here and behind the in-call
 * overlay.
 */
public object SupportDestination {
    public const val ROUTE: String = "HelpScreen"
    public const val FLOW: String = "support"
    public const val ROOT_TEST_TAG: String = "support_root"

    /** Excluded from capture — see the class comment. */
    public const val IS_CAPTURED: Boolean = false
}
