package com.vyaparpay.core.screencontext

/**
 * One resolved anchor node after pruning and text-run merging: a node whose
 * own properties (plus whatever pure-text siblings/descendants attached to
 * it) resolved to a role, ready for `SemanticSnapshotBuilder` rule 4 (extract
 * state) to turn into a [ScreenComponent].
 *
 * [textParts] is deliberately an ordered list, not a pre-joined string: rule
 * 4's per-role extraction needs to reason about which part is the label and
 * which is the value (e.g. `recipient`'s "To" / "Amazon Business" pair, or
 * `amount_field`'s currency-symbol-vs-label split), which requires the parts
 * to stay distinguishable until the role is known.
 */
internal data class MergedAnchor(
    val node: RawSemanticsNode,
    val role: ScreenComponentRole,
    val textParts: List<String>,
    val editableText: String?,
)

private sealed interface WalkItem

private data class ComponentCandidate(
    val node: RawSemanticsNode,
    val role: ScreenComponentRole,
    var textParts: List<String>,
    var editableText: String?,
) : WalkItem

private data class TextRun(val parts: List<String>, val editableText: String?) : WalkItem

/**
 * docs/07 §5 rules 1 (prune) + 2 (merge text runs) + 3 (map to roles),
 * combined into one recursive walk.
 *
 * Role resolution (rule 3) has to happen *during* the merge, not after it,
 * because a node that carries an action/role signal but resolves to none of
 * the sixteen roles must vanish entirely — including its own text — rather
 * than bubble that text into an unrelated sibling. The worked example that
 * makes this concrete: `PaymentScreen`'s "Navigate up" back button
 * (`Role.Button`, a `ContentDescription`, `OnClick`, but no testTag and not
 * in "the bottom action area") is anchor-*shaped* but unmappable. If its
 * `ContentDescription` were treated as plain mergeable text, it would flow
 * into whichever field happens to be its next sibling — a real, wrong
 * information leak, not a cosmetic bug. Rule 3's own words: "unmappable
 * nodes that survive rule 1 are folded into their nearest mapped ancestor's
 * label." Reading that literally would still risk leaking into a *distant*
 * mapped ancestor several levels up; this walk instead drops an unmappable
 * anchor-shaped node's own contribution outright while still passing through
 * any components its children *did* resolve to (a mapped ancestor's own
 * unmappable-but-anchor-shaped descendant does not poison the ancestor
 * either) — the conservative reading of the same rule.
 *
 * **Sibling text attachment.** Pure-text nodes with no anchor signal of their
 * own (a bare `Text("Amount")`, a bare `Text("₹")`) do not necessarily live
 * *inside* the node they describe — docs/07 §1's own raw tree has "₹" and
 * "Amount" as flat siblings of the `amount_input` `TextField`, not its
 * children (Material3's `OutlinedTextField` lays prefix/label out alongside
 * the field, not nested under it). This walk attaches a run of trailing pure
 * text to the *closest preceding* anchor in the same flattened sibling group
 * — the convention that reproduces docs/07 §1's amount field exactly (`61`
 * absorbs `63`/`64`) without also mis-attaching `71`'s "To"/"Amazon Business"
 * across the group boundary. A pure-text run with no preceding anchor at its
 * level (a bare screen heading like "Pay a vendor") has nothing to attach to
 * and is dropped, matching docs/07 §3's canonical example, which does not
 * carry a heading component.
 */
internal object SemanticsTreeMerger {

    fun merge(root: RawSemanticsNode): List<MergedAnchor> =
        walk(root).filterIsInstance<ComponentCandidate>().map {
            MergedAnchor(it.node, it.role, it.textParts, it.editableText)
        }

    private fun walk(node: RawSemanticsNode): List<WalkItem> {
        // Rule 1, subtree-aware: an invisible/zero-size parent removes its
        // whole subtree in one cut, no per-child re-check needed.
        if (!node.isVisible || node.isZeroSize) return emptyList()

        val childItems = node.children.flatMap { walk(it) }
        val (childComponents, leadingText) = attachTrailingText(childItems)

        val candidate = isAnchorCandidate(node)
        val resolvedRole = if (candidate) RoleMapper.resolve(node) else null

        return when {
            resolvedRole != null -> {
                val parts = node.textRuns + node.contentDescription + (leadingText?.parts ?: emptyList())
                val editable = node.editableText ?: leadingText?.editableText
                listOf(ComponentCandidate(node, resolvedRole, parts, editable)) + childComponents
            }
            candidate -> {
                // Anchor-shaped but unmappable: drop this node's own text and
                // any leftover leading text from its children (see class
                // kdoc), but resolved descendant components still pass through.
                childComponents
            }
            hasOwnText(node) || leadingText != null -> {
                val parts = node.textRuns + node.contentDescription + (leadingText?.parts ?: emptyList())
                val editable = node.editableText ?: leadingText?.editableText
                listOf(TextRun(parts, editable)) + childComponents
            }
            else -> childComponents
        }
    }

    private fun hasOwnText(node: RawSemanticsNode): Boolean =
        node.textRuns.isNotEmpty() || node.contentDescription.isNotEmpty()

    /**
     * Splits a flattened sibling group into its finalized [ComponentCandidate]s
     * (each with any trailing [TextRun]s already folded in, backward-attach —
     * see class kdoc) and whatever [TextRun] precedes the first anchor in the
     * group, which the caller must decide what to do with (attach to itself
     * if it is an anchor; drop otherwise).
     */
    private fun attachTrailingText(items: List<WalkItem>): Pair<List<ComponentCandidate>, TextRun?> {
        val result = mutableListOf<ComponentCandidate>()
        var leading: TextRun? = null
        for (item in items) {
            when (item) {
                is ComponentCandidate -> result += item
                is TextRun -> {
                    if (result.isEmpty()) {
                        leading = leading?.let { TextRun(it.parts + item.parts, it.editableText ?: item.editableText) }
                            ?: item
                    } else {
                        val last = result.last()
                        last.textParts = last.textParts + item.parts
                        if (last.editableText == null) last.editableText = item.editableText
                    }
                }
            }
        }
        return result to leading
    }
}
