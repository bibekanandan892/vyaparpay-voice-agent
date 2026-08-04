package com.vyaparpay.core.screencontext

import com.vyaparpay.core.analytics.AppEvent
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The golden test: a representative [RawSemanticsTree] fixture covering the
 * nodes docs/07-ui-semantic-context.md §1's `PaymentScreen` excerpt actually
 * lists (amount field, recipient row, Pay Now CTA, the "Payment Failed"
 * snackbar, the "Daily Limit Exceeded" dialog, plus prune-worthy decorative
 * nodes and an unmappable back button), transformed end to end through
 * [SemanticSnapshotBuilder] and compared field-for-field against
 * `protocol/fixtures/context/screen_context_payment_decline.json` (T1,
 * already merged — the JSON below is copied verbatim from that file; keep
 * the two in sync by hand, since Android tests have no wired-up path to read
 * `protocol/` fixtures directly — see `.github/workflows/android.yml`'s own comment
 * on why: "nothing wires those fixtures in yet... the day they land, the
 * coupling is already honest." This test is that day, for this one fixture).
 *
 * Comparison is structural ([kotlinx.serialization.json.JsonObject] equality
 * is a `Map` equality — field-for-field, order-independent) rather than raw
 * byte-string comparison, per this task's own "byte-for-byte, or field-for-
 * field if impractical" allowance: `JsonObject` equality is strictly more
 * rigorous than a formatting-sensitive string compare.
 */
class SemanticSnapshotBuilderTest {

    private val canonicalJson = """
        {
          "v": "screen_context/v1",
          "screen": "PaymentScreen",
          "flow": "vendor_payment",
          "components": [
            {"role": "amount_field", "label": "Amount", "value": "₹245"},
            {"role": "recipient", "label": "To", "value": "Amazon Business"},
            {"role": "primary_cta", "label": "Pay Now", "enabled": true},
            {"role": "dialog", "label": "Daily Limit Exceeded", "visible": true},
            {"role": "snackbar", "label": "Payment Failed", "visible": true}
          ],
          "last_action": {"type": "tap", "target": "Pay Now", "ts": 1784536440000},
          "last_api": {"method": "POST", "path": "/payments", "status": 402,
                       "error_code": "DAILY_LIMIT_EXCEEDED"},
          "dirty_fields": [], "loading": false
        }
    """.trimIndent()

    /**
     * A representative walk of `PaymentScreen`, covering: the amount field
     * with its flat "₹"/"Amount" siblings (docs/07 §1's own worked example
     * for rule 2's merge); the recipient row with its unmappable "Change
     * recipient" chevron child; a decorative interop placeholder (rule 1
     * prune); an unmappable "Navigate up" back button + screen heading
     * (rule 1/3 prune, proving neither leaks into a sibling); the Pay Now
     * CTA.
     *
     * **Corrected topology (HIGH-1 fix, independent review of 429f551).**
     * This fixture used to model the snackbar as its OWN separate root,
     * placed last in [RawSemanticsTree.roots] — `[main, dialog, snackbar]` —
     * purely to reproduce the canonical fixture's given component order. The
     * review traced through the REAL `PaymentScreen.kt` and found that is
     * not how Compose actually windows these two: `Snackbar` renders
     * in-tree, as the last sibling inside the screen's own root `Box`
     * (alongside `Scaffold`), while `AlertDialog` opens a genuinely separate
     * window, attached to `UiTreeCollector.attachedRoots` only after the
     * main root. The real topology is therefore `[main root (ending in the
     * in-tree snackbar), dialog root]` — snackbar attached to the SAME root
     * as the fields/CTA, dialog as its own later root — which is what this
     * fixture now builds. Naively walked, that topology produces `[...,
     * snackbar, dialog]` (the reverse of the canonical example);
     * [SemanticSnapshotBuilder.build]'s `reconcileDialogBeforeSnackbar` (the
     * HIGH-1 fix) is what reconciles it back to `[..., dialog, snackbar]`
     * before this test's assertions ever see it — so this fixture, unlike
     * before, exercises that guard rather than sidestepping it.
     */
    private fun paymentScreenTree(): RawSemanticsTree {
        val backButton = RawSemanticsNode(
            id = 53,
            role = RawRole.BUTTON,
            contentDescription = listOf("Navigate up"),
            hasOnClick = true,
        )
        val header = RawSemanticsNode(id = 55, textRuns = listOf("Pay Vendor"))
        val headerRow = RawSemanticsNode(id = 52, children = listOf(backButton, header))

        val amountField = RawSemanticsNode(id = 61, testTag = "amount_input", editableText = "245", focused = false)
        val currency = RawSemanticsNode(id = 63, textRuns = listOf("₹"))
        val amountLabel = RawSemanticsNode(id = 64, textRuns = listOf("Amount"))

        val changeRecipientChevron = RawSemanticsNode(
            id = 75,
            role = RawRole.BUTTON,
            contentDescription = listOf("Change recipient"),
            // No OnClick per docs/07 §1's own raw-tree excerpt for this node.
        )
        val recipient = RawSemanticsNode(
            id = 71,
            testTag = "recipient_row",
            hasOnClick = true,
            children = listOf(
                RawSemanticsNode(id = 72, textRuns = listOf("To")),
                RawSemanticsNode(id = 74, textRuns = listOf("Amazon Business")),
                changeRecipientChevron,
            ),
        )

        // The legacy fee-breakdown AndroidView interop island (docs/07 §1
        // node 88, §2.2) — no semantics of its own in this fixture, a
        // decorative rule-1 prune target.
        val interopPlaceholder = RawSemanticsNode(id = 88)

        val payNowCta = RawSemanticsNode(
            id = 102,
            testTag = "pay_now_cta",
            role = RawRole.BUTTON,
            textRuns = listOf("Pay Now"),
            enabled = true,
            hasOnClick = true,
        )

        // In-tree, real topology (see class kdoc above): the Snackbar is a
        // sibling of screenRoot inside PaymentScreen's own Box, the LAST
        // element under the main root — not a separate root.
        val snackbarNode = RawSemanticsNode(
            id = 119,
            testTag = "payment_snackbar",
            liveRegion = RawLiveRegion.POLITE,
            textRuns = listOf("Payment Failed"),
        )

        val screenRoot = RawSemanticsNode(
            id = 47,
            testTag = "payment_screen_root", // present, but resolves via TestTagRoles to UNRESOLVED — an unmappable ancestor, not an accident
            children = listOf(headerRow, amountField, currency, amountLabel, recipient, interopPlaceholder, payNowCta),
        )
        val mainRoot = RawSemanticsNode(id = 1, children = listOf(screenRoot, snackbarNode))

        val dialogBody = RawSemanticsNode(id = 132, textRuns = listOf("Your daily transaction limit has been reached."))
        val dialogRoot = RawSemanticsNode(
            id = 131,
            testTag = "limit_dialog",
            isDialog = true,
            paneTitle = "Daily Limit Exceeded",
            children = listOf(dialogBody),
        )

        // Attach order: main root first (never detached while the screen is
        // on-screen), dialog root second (attached once state.declineDialog
        // becomes non-null) — matching UiTreeCollector.attachedRoots' real
        // attach-order contract (RawSemanticsTree.kt's own kdoc).
        return RawSemanticsTree(roots = listOf(mainRoot, dialogRoot))
    }

    /** docs/08 §2.4's own worked timeline for this exact incident, newest-first (matching `EventTracker.recent()`). */
    private fun paymentDeclineEvents(): List<AppEvent> = listOf(
        AppEvent.Dialog(name = "Daily Limit Exceeded", ts = 1_784_536_440_900L, visible = true),
        AppEvent.ApiErrorEvent(name = "POST /payments", ts = 1_784_536_440_820L, status = 402, code = "DAILY_LIMIT_EXCEEDED"),
        // trackedClickable actually records testTag, not label — see
        // SemanticSnapshotBuilder's own kdoc on this known discrepancy;
        // "Pay Now" is supplied directly here to match the canonical fixture.
        AppEvent.Tap(name = "Pay Now", ts = 1_784_536_440_000L, screen = "PaymentScreen"),
        AppEvent.Tap(name = "Amazon Business", ts = 1_784_536_428_000L, screen = "PaymentScreen"),
        AppEvent.Input(name = "amount", ts = 1_784_536_417_000L, value = "₹245"),
        AppEvent.Nav(name = "PaymentScreen", ts = 1_784_536_395_000L, from = "DashboardScreen"),
    )

    @Test
    fun `the canonical PaymentScreen decline snapshot matches protocol screen_context_payment_decline field-for-field`() {
        val ir = SemanticSnapshotBuilder.build(
            tree = paymentScreenTree(),
            screen = "PaymentScreen",
            flow = "vendor_payment",
            recentEvents = paymentDeclineEvents(),
        )

        val expected = Json.parseToJsonElement(canonicalJson)
        assertEquals(expected, ir.toJson())
    }

    @Test
    fun `the golden snapshot fits comfortably inside the 300-token budget`() {
        val ir = SemanticSnapshotBuilder.build(
            tree = paymentScreenTree(),
            screen = "PaymentScreen",
            flow = "vendor_payment",
            recentEvents = paymentDeclineEvents(),
        )

        val tokens = DropLadder.estimateTokens(DropLadder.compactJson(ir.toJson()))
        assertTrue("expected <=300 tokens, was $tokens", tokens <= DropLadder.SNAPSHOT_TOKEN_BUDGET)
    }

    // -- Rule 5: rank and cap --------------------------------------------------

    @Test
    fun `under the 20-component cap, components stay in pure traversal order`() {
        val ir = SemanticSnapshotBuilder.build(paymentScreenTree(), "PaymentScreen", "vendor_payment", emptyList())

        assertEquals(
            listOf(
                ScreenComponentRole.AMOUNT_FIELD,
                ScreenComponentRole.RECIPIENT,
                ScreenComponentRole.PRIMARY_CTA,
                ScreenComponentRole.DIALOG,
                ScreenComponentRole.SNACKBAR,
            ),
            ir.components.map { it.role },
        )
    }

    @Test
    fun `over the 20-component cap, low-priority components are dropped first, survivors keep traversal order`() {
        // 25 low-priority (tier 6/7) toggles, plus one dialog (tier 1) placed
        // LAST in traversal order — if rank governs only cap-priority (not a
        // full reorder), the dialog must still survive (it outranks the
        // toggles for retention) but must still land at the END of the
        // output array, not jump to the front.
        val toggles = (1..25).map {
            RawSemanticsNode(id = it, testTag = null, toggleableState = RawToggleableState.OFF, textRuns = listOf("Toggle $it"))
        }
        val dialog = RawSemanticsNode(id = 100, isDialog = true, paneTitle = "Late Dialog")
        val root = RawSemanticsNode(id = 0, children = toggles + dialog)

        val ir = SemanticSnapshotBuilder.build(RawSemanticsTree(listOf(root)), "Screen", "flow", emptyList())

        assertEquals(20, ir.components.size)
        assertTrue(ir.components.any { it.role == ScreenComponentRole.DIALOG })
        // The dialog is still the LAST surviving component (traversal-order
        // preserving), not moved to the front despite being tier 1.
        assertEquals(ScreenComponentRole.DIALOG, ir.components.last().role)
    }

    @Test
    fun `a dialog that traverses after a snackbar is moved to immediately before it`() {
        // HIGH-1 fix (independent review of 429f551): a real capture of a
        // screen where the dialog is a separate, later-attached root and the
        // snackbar is in-tree produces raw traversal order [..., snackbar,
        // dialog] -- reconcileDialogBeforeSnackbar must correct that to
        // [..., dialog, snackbar] to match docs/07 §3's canonical example.
        // Modeled directly here (not via paymentScreenTree()) so this guard
        // has its own unit-level proof, independent of the golden fixture.
        val cta = RawSemanticsNode(id = 1, testTag = "pay_now_cta", role = RawRole.BUTTON, hasOnClick = true, textRuns = listOf("Pay Now"))
        val snackbarNode = RawSemanticsNode(id = 2, testTag = "payment_snackbar", liveRegion = RawLiveRegion.POLITE, textRuns = listOf("Payment Failed"))
        val mainRoot = RawSemanticsNode(id = 0, children = listOf(cta, snackbarNode))
        val dialogRoot = RawSemanticsNode(id = 3, isDialog = true, paneTitle = "Daily Limit Exceeded")

        val ir = SemanticSnapshotBuilder.build(RawSemanticsTree(listOf(mainRoot, dialogRoot)), "PaymentScreen", "vendor_payment", emptyList())

        assertEquals(
            listOf(ScreenComponentRole.PRIMARY_CTA, ScreenComponentRole.DIALOG, ScreenComponentRole.SNACKBAR),
            ir.components.map { it.role },
        )
    }

    @Test
    fun `an error_banner that traverses after a snackbar is also moved to immediately before it`() {
        // The same guard covers error_banner, not just dialog (both are tier 1).
        val snackbarNode = RawSemanticsNode(id = 1, testTag = "payment_snackbar", liveRegion = RawLiveRegion.POLITE, textRuns = listOf("Payment Failed"))
        val errorBanner = RawSemanticsNode(id = 2, liveRegion = RawLiveRegion.ASSERTIVE, textRuns = listOf("Network unreachable"))
        val root = RawSemanticsNode(id = 0, children = listOf(snackbarNode, errorBanner))

        val ir = SemanticSnapshotBuilder.build(RawSemanticsTree(listOf(root)), "Screen", "flow", emptyList())

        assertEquals(
            listOf(ScreenComponentRole.ERROR_BANNER, ScreenComponentRole.SNACKBAR),
            ir.components.map { it.role },
        )
    }

    @Test
    fun `a dialog that already precedes the snackbar is left untouched`() {
        val dialog = RawSemanticsNode(id = 1, isDialog = true, paneTitle = "Daily Limit Exceeded")
        val snackbarNode = RawSemanticsNode(id = 2, testTag = "payment_snackbar", liveRegion = RawLiveRegion.POLITE, textRuns = listOf("Payment Failed"))
        val root = RawSemanticsNode(id = 0, children = listOf(dialog, snackbarNode))

        val ir = SemanticSnapshotBuilder.build(RawSemanticsTree(listOf(root)), "Screen", "flow", emptyList())

        assertEquals(listOf(ScreenComponentRole.DIALOG, ScreenComponentRole.SNACKBAR), ir.components.map { it.role })
    }

    @Test
    fun `list visible_count reflects the truncated list_items, not the pre-truncation count`() {
        // HIGH-2 fix (independent review of 429f551): fillListVisibleCounts
        // must run AFTER the 20-component structural cap, not before, or
        // visible_count overstates what actually survives in `components`.
        val cta = RawSemanticsNode(id = 0, testTag = "pay_now_cta", role = RawRole.BUTTON, hasOnClick = true, textRuns = listOf("Pay Now"))
        val items = (1..25).map { i -> RawSemanticsNode(id = i + 1, isListItem = true, textRuns = listOf("Batch $i")) }
        val list = RawSemanticsNode(id = 1, collectionInfo = RawCollectionInfo(totalCount = 25), children = items)
        val root = RawSemanticsNode(id = 100, children = listOf(cta, list))

        val ir = SemanticSnapshotBuilder.build(RawSemanticsTree(listOf(root)), "Screen", "flow", emptyList())

        assertEquals(20, ir.components.size)
        val survivingItemCount = ir.components.count { it.role == ScreenComponentRole.LIST_ITEM }
        assertTrue("expected some list_items to have been dropped by the cap", survivingItemCount < 25)
        val listComponent = ir.components.filterIsInstance<ScreenComponent.ListComponent>().single()
        assertEquals(
            "visible_count must match what actually survived truncation, not the pre-truncation count",
            survivingItemCount,
            listComponent.visibleCount,
        )
        assertEquals(25, listComponent.totalCount) // total_count is unaffected -- it comes straight from CollectionInfo
    }

    // -- Rule 6: redact at source ------------------------------------------

    @Test
    fun `a testTag suffixed _sensitive redacts its value`() {
        val cardField = RawSemanticsNode(id = 1, testTag = "card_number_sensitive", editableText = "4111111111111111", isSensitive = true)

        val ir = SemanticSnapshotBuilder.build(RawSemanticsTree(listOf(cardField)), "CardScreen", "wallet", emptyList())

        val field = ir.components.single() as ScreenComponent.TextField
        assertEquals("[REDACTED]", field.value)
    }

    @Test
    fun `a value that merely looks like a card number is redacted even without the sensitive marker`() {
        // docs/07 §5 rule 6: "plus pattern matching as backstop" — independent of isSensitive.
        val unmarkedField = RawSemanticsNode(id = 1, testTag = "some_field", editableText = "4111 1111 1111 1111")

        val ir = SemanticSnapshotBuilder.build(RawSemanticsTree(listOf(unmarkedField)), "Screen", "flow", emptyList())

        val field = ir.components.single() as ScreenComponent.TextField
        assertEquals("[REDACTED]", field.value)
    }

    // CRITICAL-1 fix (independent review of 429f551): the two tests above
    // both happen to resolve to TEXT_FIELD, the one role that was already
    // redaction-checked before this fix — passing tests alone did not prove
    // redaction was safe across the other fourteen roles. These three cover
    // the review's concrete failure scenario and the two other role shapes
    // (a single-node label+value pair, and a message-only role) it named.

    @Test
    fun `a RECIPIENT resolved via the vendor_ prefix redacts a sensitive account number`() {
        // The review's concrete failure: a field tagged
        // vendor_account_number_sensitive prefix-matches TestTagRoles.roleFor
        // to RECIPIENT (not TEXT_FIELD) before sensitivity is ever
        // consulted. Before the fix, RECIPIENT's value went through plain
        // normalize() with no redaction check at all.
        val label = RawSemanticsNode(id = 2, textRuns = listOf("Account"))
        val value = RawSemanticsNode(id = 3, textRuns = listOf("1234567890123456"))
        val recipientRow = RawSemanticsNode(
            id = 1,
            testTag = "vendor_account_number_sensitive",
            hasOnClick = true,
            isSensitive = true,
            children = listOf(label, value),
        )

        val ir = SemanticSnapshotBuilder.build(RawSemanticsTree(listOf(recipientRow)), "Screen", "flow", emptyList())

        val recipient = ir.components.single() as ScreenComponent.Recipient
        assertEquals(ScreenComponentRole.RECIPIENT, recipient.role)
        assertEquals("[REDACTED]", recipient.value)
    }

    @Test
    fun `a DIALOG label is redacted when the dialog body itself reveals a card number`() {
        // dialog carries only a `label` (no separate value field), so a
        // message-shaped role must have ITS label redaction-checked too --
        // not just roles with a distinct editable value.
        val dialogBody = RawSemanticsNode(id = 2, textRuns = listOf("Card 4111111111111111 was declined"))
        val dialogRoot = RawSemanticsNode(id = 1, isDialog = true, children = listOf(dialogBody))

        val ir = SemanticSnapshotBuilder.build(RawSemanticsTree(listOf(dialogRoot)), "Screen", "flow", emptyList())

        val dialog = ir.components.single() as ScreenComponent.Dialog
        assertEquals("[REDACTED]", dialog.label)
    }

    @Test
    fun `a LIST_ITEM value is redacted when it looks like a card number`() {
        val itemLabel = RawSemanticsNode(id = 3, textRuns = listOf("Vendor account"))
        val itemValue = RawSemanticsNode(id = 4, textRuns = listOf("4111111111111111"))
        val listItem = RawSemanticsNode(id = 2, isListItem = true, children = listOf(itemLabel, itemValue))
        val list = RawSemanticsNode(id = 1, collectionInfo = RawCollectionInfo(totalCount = 1), children = listOf(listItem))

        val ir = SemanticSnapshotBuilder.build(RawSemanticsTree(listOf(list)), "Screen", "flow", emptyList())

        val item = ir.components.filterIsInstance<ScreenComponent.ListItem>().single()
        assertEquals("Vendor account", item.label) // the label itself is not sensitive-shaped -- only the value is
        assertEquals("[REDACTED]", item.value)
    }

    // CRITICAL-2 fix (independent review of 429f551): docs/07 §5 rule 6 also
    // names CVV and PIN, which had zero backstop regex coverage. See
    // PIN_LIKE's kdoc for why the backstop is context-gated (label/testTag
    // must name the field a PIN/CVV/OTP) rather than a bare digit-count
    // match, and why the `_sensitive` marker remains the primary defense.

    @Test
    fun `a 4-digit value is redacted when its own label names it a PIN`() {
        val pinField = RawSemanticsNode(id = 1, textRuns = listOf("PIN"), editableText = "1234")

        val ir = SemanticSnapshotBuilder.build(RawSemanticsTree(listOf(pinField)), "Screen", "flow", emptyList())

        // Resolves via the amount_field bare-digit heuristic (RoleMapper's
        // CURRENCY_LIKE fallback matches any short bare digit run) -- role
        // resolution is a separate concern from CRITICAL-2; what matters
        // here is that whichever role it lands in, the value redacts.
        val value = (ir.components.single() as ScreenComponent.AmountField).value
        assertEquals("[REDACTED]", value)
    }

    @Test
    fun `a 3-digit value is redacted when its own label names it a CVV`() {
        val cvvField = RawSemanticsNode(id = 1, textRuns = listOf("CVV"), editableText = "123")

        val ir = SemanticSnapshotBuilder.build(RawSemanticsTree(listOf(cvvField)), "Screen", "flow", emptyList())

        val value = (ir.components.single() as ScreenComponent.AmountField).value
        assertEquals("[REDACTED]", value)
    }

    @Test
    fun `a short digit value with no PIN or CVV context is NOT redacted`() {
        // The false-positive guard PIN_LIKE's kdoc documents: an ordinary
        // 4-digit balance/count with no PIN/CVV-labeled context must not be
        // swept up by the backstop, or the IR would be noisy with false
        // redactions on completely ordinary screens.
        val quantityField = RawSemanticsNode(id = 1, testTag = "settled_balance", textRuns = listOf("Settled", "5000"))

        val ir = SemanticSnapshotBuilder.build(RawSemanticsTree(listOf(quantityField)), "Screen", "flow", emptyList())

        val value = (ir.components.single() as ScreenComponent.BalanceDisplay).value
        assertEquals("5000", value)
    }

    @Test
    fun `a PIN-shaped value redacts via the sensitive marker even with no PIN label`() {
        // The marker is authoritative regardless of the labeled-context
        // backstop above -- a screen author who tags a field _sensitive (or
        // sets IsSensitiveData) is protected even if they never wrote "PIN"
        // anywhere in the field's own text.
        val markedField = RawSemanticsNode(id = 1, testTag = "login_pin_sensitive", editableText = "9821", isSensitive = true)

        val ir = SemanticSnapshotBuilder.build(RawSemanticsTree(listOf(markedField)), "Screen", "flow", emptyList())

        val value = (ir.components.single() as ScreenComponent.AmountField).value
        assertEquals("[REDACTED]", value)
    }

    @Test
    fun `a normal amount value is not redacted`() {
        val ir = SemanticSnapshotBuilder.build(paymentScreenTree(), "PaymentScreen", "vendor_payment", emptyList())

        val amount = ir.components.filterIsInstance<ScreenComponent.AmountField>().single()
        assertEquals("₹245", amount.value)
    }

    @Test
    fun `zero-width and control characters are stripped from labels`() {
        // U+200B zero-width space, embedded via an explicit escape so the
        // test is verifiable rather than relying on an invisible character
        // sitting in the source file.
        val zeroWidthSpace = "​"
        val poisoned = RawSemanticsNode(id = 1, testTag = "note_balance", textRuns = listOf("Balance$zeroWidthSpace"))

        val ir = SemanticSnapshotBuilder.build(RawSemanticsTree(listOf(poisoned)), "S", "f", emptyList())

        assertEquals("Balance", (ir.components.single() as ScreenComponent.BalanceDisplay).label)
    }

    @Test
    fun `fields are capped at 120 characters`() {
        // A repeated letter, not a repeated digit: since CRITICAL-1's fix
        // (independent review of 429f551) routes every role's value through
        // the same redaction backstop as text_field/amount_field, a long run
        // of digits here would trip CARD_NUMBER_LIKE and redact to
        // "[REDACTED]" before length-capping is even observable — this test
        // is about truncation, not redaction, so it uses a shape neither
        // backstop regex matches.
        val longValueNode = RawSemanticsNode(id = 1, testTag = "long_balance", textRuns = listOf("Label", "x".repeat(200)))

        val ir = SemanticSnapshotBuilder.build(RawSemanticsTree(listOf(longValueNode)), "S", "f", emptyList())

        val value = (ir.components.single() as ScreenComponent.BalanceDisplay).value
        assertEquals(120, value.length)
    }

    // -- Rule 4 (cont.): dirty_fields / loading ------------------------------

    @Test
    fun `a focused editable field is listed in dirty_fields`() {
        val focusedAmount = RawSemanticsNode(id = 1, testTag = "amount_input", editableText = "500", focused = true)

        val ir = SemanticSnapshotBuilder.build(RawSemanticsTree(listOf(focusedAmount)), "PaymentScreen", "vendor_payment", emptyList())

        assertEquals(listOf("amount_input"), ir.dirtyFields)
    }

    @Test
    fun `an unfocused field after submit is not dirty, matching the canonical example`() {
        val ir = SemanticSnapshotBuilder.build(paymentScreenTree(), "PaymentScreen", "vendor_payment", emptyList())

        assertEquals(emptyList<String>(), ir.dirtyFields)
    }

    @Test
    fun `a visible progress indicator sets loading true`() {
        val spinner = RawSemanticsNode(id = 1, hasProgressIndicator = true)
        val cta = RawSemanticsNode(id = 2, testTag = "pay_now_cta", role = RawRole.BUTTON, hasOnClick = true, textRuns = listOf("Pay Now"))
        val root = RawSemanticsNode(id = 0, children = listOf(spinner, cta))

        val ir = SemanticSnapshotBuilder.build(RawSemanticsTree(listOf(root)), "PaymentScreen", "vendor_payment", emptyList())

        assertTrue(ir.loading)
    }

    @Test
    fun `a pruned (invisible) progress indicator does not set loading`() {
        val hiddenSpinner = RawSemanticsNode(id = 1, hasProgressIndicator = true, isVisible = false)

        val ir = SemanticSnapshotBuilder.build(RawSemanticsTree(listOf(hiddenSpinner)), "S", "f", emptyList())

        assertEquals(false, ir.loading)
    }

    // -- Rule 7: last_action / last_api --------------------------------------

    @Test
    fun `last_action skips api_error and dialog events, taking the newest user action`() {
        val ir = SemanticSnapshotBuilder.build(paymentScreenTree(), "PaymentScreen", "vendor_payment", paymentDeclineEvents())

        assertEquals("tap", ir.lastAction?.type)
        assertEquals("Pay Now", ir.lastAction?.target)
        assertEquals(1_784_536_440_000L, ir.lastAction?.ts)
    }

    @Test
    fun `last_api stops scanning at the most recent nav, treating an older error as belonging to a previous screen`() {
        val eventsAfterLeavingTheErrorScreen = listOf(
            AppEvent.Nav(name = "PaymentScreen", ts = 2_000L, from = "SettlementsScreen"),
            AppEvent.ApiErrorEvent(name = "GET /settlements", ts = 1_000L, status = 500, code = "INTERNAL"),
        )

        val ir = SemanticSnapshotBuilder.build(paymentScreenTree(), "PaymentScreen", "vendor_payment", eventsAfterLeavingTheErrorScreen)

        assertNull(ir.lastApi)
    }

    @Test
    fun `last_api finds an error that occurred after the most recent nav`() {
        val events = listOf(
            AppEvent.ApiErrorEvent(name = "POST /payments", ts = 2_000L, status = 402, code = "DAILY_LIMIT_EXCEEDED"),
            AppEvent.Nav(name = "PaymentScreen", ts = 1_000L, from = "DashboardScreen"),
        )

        val ir = SemanticSnapshotBuilder.build(paymentScreenTree(), "PaymentScreen", "vendor_payment", events)

        assertEquals("POST", ir.lastApi?.method)
        assertEquals("/payments", ir.lastApi?.path)
        assertEquals(402, ir.lastApi?.status)
        assertEquals("DAILY_LIMIT_EXCEEDED", ir.lastApi?.errorCode)
    }
}
