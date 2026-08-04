package com.vyaparpay.core.screencontext

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** docs/07 §5 rules 1 (prune) + 2 (merge text runs) + 3 (map to roles). */
class SemanticsTreeMergerTest {

    private fun leaf(
        id: Int,
        testTag: String? = null,
        textRuns: List<String> = emptyList(),
        contentDescription: List<String> = emptyList(),
        editableText: String? = null,
        role: RawRole? = null,
        hasOnClick: Boolean = false,
        isDialog: Boolean = false,
        isVisible: Boolean = true,
        isZeroSize: Boolean = false,
        children: List<RawSemanticsNode> = emptyList(),
    ) = RawSemanticsNode(
        id = id,
        testTag = testTag,
        textRuns = textRuns,
        contentDescription = contentDescription,
        editableText = editableText,
        role = role,
        hasOnClick = hasOnClick,
        isDialog = isDialog,
        isVisible = isVisible,
        isZeroSize = isZeroSize,
        children = children,
    )

    @Test
    fun `an invisible node and its whole subtree are pruned`() {
        val invisibleParent = leaf(
            id = 1,
            isVisible = false,
            children = listOf(leaf(id = 2, testTag = "amount_input", editableText = "245")),
        )

        assertEquals(emptyList<MergedAnchor>(), SemanticsTreeMerger.merge(invisibleParent))
    }

    @Test
    fun `a zero-size node is pruned`() {
        val zeroSize = leaf(id = 1, testTag = "amount_input", editableText = "245", isZeroSize = true)

        assertEquals(emptyList<MergedAnchor>(), SemanticsTreeMerger.merge(zeroSize))
    }

    @Test
    fun `a decorative node with no text, action, or description is pruned`() {
        val decorative = leaf(id = 1) // no testTag, no text, no description, no action
        val root = leaf(id = 0, children = listOf(decorative))

        assertEquals(emptyList<MergedAnchor>(), SemanticsTreeMerger.merge(root))
    }

    @Test
    fun `a bare screen heading with no anchor sibling is dropped, not merged into anything`() {
        val heading = leaf(id = 1, textRuns = listOf("Pay a vendor"))
        val cta = leaf(id = 2, testTag = "pay_now_cta", role = RawRole.BUTTON, hasOnClick = true, textRuns = listOf("Pay Now"))
        val root = leaf(id = 0, children = listOf(heading, cta))

        val result = SemanticsTreeMerger.merge(root)

        assertEquals(1, result.size)
        assertEquals(listOf("Pay Now"), result.single().textParts)
    }

    @Test
    fun `trailing pure-text siblings merge into the nearest preceding anchor, not a later one`() {
        // Reproduces docs/07 §1's amount field exactly: the TextField anchor
        // (61) comes first, "₹" (63) and "Amount" (64) are flat siblings
        // AFTER it, then an unrelated recipient anchor (71) follows — the
        // currency/label text must attach to the TextField, never leak into
        // the recipient.
        val amountField = leaf(id = 61, testTag = "amount_input", editableText = "245")
        val currency = leaf(id = 63, textRuns = listOf("₹"))
        val amountLabel = leaf(id = 64, textRuns = listOf("Amount"))
        val recipient = leaf(
            id = 71,
            testTag = "recipient_row",
            hasOnClick = true,
            children = listOf(leaf(id = 72, textRuns = listOf("To")), leaf(id = 74, textRuns = listOf("Amazon Business"))),
        )
        val root = leaf(id = 47, children = listOf(amountField, currency, amountLabel, recipient))

        val result = SemanticsTreeMerger.merge(root)

        assertEquals(2, result.size)
        val amount = result[0]
        assertEquals(ScreenComponentRole.AMOUNT_FIELD, amount.role)
        assertEquals(listOf("₹", "Amount"), amount.textParts)
        assertEquals("245", amount.editableText)
        val recipientAnchor = result[1]
        assertEquals(ScreenComponentRole.RECIPIENT, recipientAnchor.role)
        assertEquals(listOf("To", "Amazon Business"), recipientAnchor.textParts)
    }

    @Test
    fun `an anchor-shaped but unmappable node is dropped without leaking its text into a sibling`() {
        // docs/07 §1's "Navigate up" back button: Role.Button + ContentDescription
        // + OnClick, no testTag, not in "the bottom action area" — unmappable.
        // Its ContentDescription must not bleed into the next real anchor.
        val backButton = leaf(id = 53, role = RawRole.BUTTON, contentDescription = listOf("Navigate up"), hasOnClick = true)
        val header = leaf(id = 55, textRuns = listOf("Pay Vendor"))
        val headerRow = leaf(id = 52, children = listOf(backButton, header))
        val cta = leaf(id = 102, testTag = "pay_now_cta", role = RawRole.BUTTON, hasOnClick = true, textRuns = listOf("Pay Now"))
        val root = leaf(id = 47, children = listOf(headerRow, cta))

        val result = SemanticsTreeMerger.merge(root)

        assertEquals(1, result.size)
        assertEquals(listOf("Pay Now"), result.single().textParts)
        assertTrue("Navigate up" !in result.single().textParts)
    }

    @Test
    fun `a resolvable ancestor absorbs its own unmapped descendant text but nested anchors still surface separately`() {
        val dismissButton = leaf(id = 3, testTag = "daily_limit_dismiss_secondary", textRuns = listOf("Got it"))
        val body = leaf(id = 2, textRuns = listOf("Your daily transaction limit has been reached."))
        val dialog = leaf(id = 1, testTag = "limit_dialog", isDialog = true, children = listOf(body, dismissButton))

        val result = SemanticsTreeMerger.merge(dialog)

        assertEquals(2, result.size)
        assertEquals(ScreenComponentRole.DIALOG, result[0].role)
        assertEquals(ScreenComponentRole.SECONDARY_CTA, result[1].role)
        assertEquals(listOf("Got it"), result[1].textParts)
    }
}
