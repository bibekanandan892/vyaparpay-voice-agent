package com.vyaparpay.core.ui.testtag

import org.junit.Assert.assertEquals
import org.junit.Test

class TestTagRolesTest {

    @Test
    fun `the docs 03 section 4 examples all resolve to their documented role`() {
        val expected = mapOf(
            "pay_now_cta" to TestTagRole.PRIMARY_CTA,
            "cancel_secondary" to TestTagRole.SECONDARY_CTA,
            "amount_input" to TestTagRole.AMOUNT_FIELD,
            "recipient_row" to TestTagRole.RECIPIENT,
            "wallet_balance" to TestTagRole.BALANCE_DISPLAY,
            "settlement_status" to TestTagRole.STATUS_BADGE,
            "payment_snackbar" to TestTagRole.SNACKBAR,
            "pin_error" to TestTagRole.ERROR_BANNER,
            "kyc_alert" to TestTagRole.ALERT_BANNER,
            "receipt_image" to TestTagRole.IMAGE,
        )

        expected.forEach { (tag, role) ->
            assertEquals("tag `$tag`", role, TestTagRoles.roleFor(tag))
        }
    }

    @Test
    fun `a tag that follows no convention is unresolved rather than guessed at`() {
        assertEquals(TestTagRole.UNRESOLVED, TestTagRoles.roleFor("box42"))
    }

    @Test
    fun `the vendor prefix is a recipient, matching the vendor-payment flow`() {
        assertEquals(TestTagRole.RECIPIENT, TestTagRoles.roleFor("vendor_name"))
    }

    @Test
    fun `every role has a distinct wire name`() {
        val wireNames = TestTagRole.entries.map { it.wireName }
        assertEquals(wireNames.size, wireNames.toSet().size)
    }
}
