package com.vyaparpay.core.screencontext

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** docs/07 §5 rule 3: tier (a) testTag, then (b) semantics properties, then (c) heuristics — first match wins. */
class RoleMapperTest {

    private fun node(
        testTag: String? = null,
        isDialog: Boolean = false,
        role: RawRole? = null,
        collectionInfo: RawCollectionInfo? = null,
        isListItem: Boolean = false,
        toggleableState: RawToggleableState? = null,
        liveRegion: RawLiveRegion? = null,
        editableText: String? = null,
        hasOnClick: Boolean = false,
        contentDescription: List<String> = emptyList(),
    ) = RawSemanticsNode(
        id = 1,
        testTag = testTag,
        isDialog = isDialog,
        role = role,
        collectionInfo = collectionInfo,
        isListItem = isListItem,
        toggleableState = toggleableState,
        liveRegion = liveRegion,
        editableText = editableText,
        hasOnClick = hasOnClick,
        contentDescription = contentDescription,
    )

    // -- Tier (a): testTag convention, via the same TestTagRoles used elsewhere ----

    @Test
    fun `testTag suffix _cta resolves to primary_cta`() {
        assertEquals(ScreenComponentRole.PRIMARY_CTA, RoleMapper.resolve(node(testTag = "pay_now_cta")))
    }

    @Test
    fun `testTag amount_input resolves to amount_field`() {
        assertEquals(ScreenComponentRole.AMOUNT_FIELD, RoleMapper.resolve(node(testTag = "amount_input")))
    }

    @Test
    fun `testTag prefix recipient_ resolves to recipient`() {
        assertEquals(ScreenComponentRole.RECIPIENT, RoleMapper.resolve(node(testTag = "recipient_row")))
    }

    @Test
    fun `an unresolved testTag falls through to tier b instead of blocking resolution`() {
        // "limit_dialog" matches no TestTagRoles convention — tier (a) must
        // fail silently and let tier (b)'s IsDialog structural check resolve
        // it, matching PaymentScreen's own documented convention (dialog
        // detection is structural, not testTag-based).
        assertEquals(ScreenComponentRole.DIALOG, RoleMapper.resolve(node(testTag = "limit_dialog", isDialog = true)))
    }

    // -- Tier (b): semantics properties, exact/structural -------------------

    @Test
    fun `IsDialog resolves to dialog regardless of testTag`() {
        assertEquals(ScreenComponentRole.DIALOG, RoleMapper.resolve(node(isDialog = true)))
    }

    @Test
    fun `Role Tab resolves to tab`() {
        assertEquals(ScreenComponentRole.TAB, RoleMapper.resolve(node(role = RawRole.TAB)))
    }

    @Test
    fun `a present ToggleableState resolves to toggle`() {
        assertEquals(ScreenComponentRole.TOGGLE, RoleMapper.resolve(node(toggleableState = RawToggleableState.ON)))
    }

    @Test
    fun `CollectionInfo resolves to list`() {
        assertEquals(ScreenComponentRole.LIST, RoleMapper.resolve(node(collectionInfo = RawCollectionInfo(47))))
    }

    @Test
    fun `CollectionItemInfo resolves to list_item`() {
        assertEquals(ScreenComponentRole.LIST_ITEM, RoleMapper.resolve(node(isListItem = true)))
    }

    @Test
    fun `tier a wins over tier b when both would match`() {
        // A node with a resolvable testTag AND IsDialog: testTag must win
        // (rule 3: "first match wins", tier a before tier b).
        val result = RoleMapper.resolve(node(testTag = "confirm_cta", isDialog = true))
        assertEquals(ScreenComponentRole.PRIMARY_CTA, result)
    }

    // -- Tier (c): heuristics -------------------------------------------------

    @Test
    fun `a polite LiveRegion resolves to snackbar`() {
        assertEquals(ScreenComponentRole.SNACKBAR, RoleMapper.resolve(node(liveRegion = RawLiveRegion.POLITE)))
    }

    @Test
    fun `an assertive LiveRegion resolves to error_banner`() {
        assertEquals(ScreenComponentRole.ERROR_BANNER, RoleMapper.resolve(node(liveRegion = RawLiveRegion.ASSERTIVE)))
    }

    @Test
    fun `editable text matching a currency pattern resolves to amount_field`() {
        assertEquals(ScreenComponentRole.AMOUNT_FIELD, RoleMapper.resolve(node(editableText = "245")))
        assertEquals(ScreenComponentRole.AMOUNT_FIELD, RoleMapper.resolve(node(editableText = "1,234.50")))
    }

    @Test
    fun `editable text that does not look like currency resolves to text_field`() {
        assertEquals(ScreenComponentRole.TEXT_FIELD, RoleMapper.resolve(node(editableText = "Search batches")))
    }

    @Test
    fun `a filled Button with an onClick and no testTag does not resolve to primary_cta`() {
        // Deliberately absent heuristic (see RoleMapper's class kdoc): a bare
        // Role.Button + OnClick is indistinguishable from navigation chrome
        // (docs/07 §1's own "Navigate up" back button carries exactly this
        // signal) — guessing it as the screen's primary action would be a
        // false positive with real consequences, not a helpful fallback.
        assertNull(RoleMapper.resolve(node(role = RawRole.BUTTON, hasOnClick = true)))
    }

    @Test
    fun `a content description with an onClick and no testTag or Role_Image does not resolve to image`() {
        // Same reasoning as the primary_cta case above: ContentDescription +
        // OnClick alone cannot distinguish a content image from a nav-chrome
        // icon button. `image` is testTag-tier only in this implementation.
        assertNull(RoleMapper.resolve(node(contentDescription = listOf("Download report"), hasOnClick = true)))
    }

    @Test
    fun `a Button with no onClick does not resolve to image or primary_cta either`() {
        // docs/07 §4: image requires ContentDescription AND OnClick; a bare
        // Role.Button with neither a testTag nor an onClick (e.g. the
        // recipient row's "Change recipient" chevron in docs/07 §1, which
        // the raw tree shows with no OnClick) must not resolve at all.
        assertNull(RoleMapper.resolve(node(role = RawRole.BUTTON, contentDescription = listOf("Change recipient"))))
    }

    @Test
    fun `a node with no signal at all resolves to nothing`() {
        assertNull(RoleMapper.resolve(node()))
    }

    // -- isAnchorCandidate ----------------------------------------------------

    @Test
    fun `a node with only text runs is not an anchor candidate`() {
        val plain = RawSemanticsNode(id = 1, textRuns = listOf("Pay a vendor"))
        assertEquals(false, isAnchorCandidate(plain))
    }

    @Test
    fun `a node with a testTag is an anchor candidate even if the tag is unresolved`() {
        assertEquals(true, isAnchorCandidate(node(testTag = "payment_screen_root")))
    }
}
