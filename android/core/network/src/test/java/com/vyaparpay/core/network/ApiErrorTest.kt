package com.vyaparpay.core.network

import org.junit.Assert.assertEquals
import org.junit.Test

class ApiErrorTest {

    @Test
    fun `a known wire code maps onto its enum constant`() {
        assertEquals(ApiError.DAILY_LIMIT_EXCEEDED, ApiError.fromWireCode("DAILY_LIMIT_EXCEEDED"))
    }

    @Test
    fun `an unrecognised wire code folds to UNKNOWN rather than throwing`() {
        assertEquals(ApiError.UNKNOWN, ApiError.fromWireCode("SOME_CODE_ADDED_NEXT_QUARTER"))
    }

    @Test
    fun `a missing wire code folds to UNKNOWN`() {
        assertEquals(ApiError.UNKNOWN, ApiError.fromWireCode(null))
    }

    @Test
    fun `UNKNOWN is never produced by matching the literal string UNKNOWN by accident`() {
        // Guards the mapping table itself: UNKNOWN is excluded from `byWireCode`,
        // so it can only ever be reached through the fallback.
        assertEquals(ApiError.UNKNOWN, ApiError.fromWireCode("UNKNOWN"))
    }

    @Test
    fun `every docs 13 §1_1 taxonomy code maps one-to-one onto its constant`() {
        // The full table, spelled out rather than derived from the enum, so a
        // constant accidentally deleted or renamed fails this test instead of
        // silently starting to fold real errors to UNKNOWN.
        val taxonomy = listOf(
            "VALIDATION_SCHEMA", "VALIDATION_UNSUPPORTED_VERSION",
            "AUTH_MISSING_TOKEN", "AUTH_INVALID_TOKEN", "AUTH_EXPIRED_TOKEN",
            "DAILY_LIMIT_EXCEEDED", "INSUFFICIENT_BALANCE",
            "SESSION_NOT_FOUND", "RESOURCE_NOT_FOUND", "SESSION_SUMMARY_PENDING",
            "SESSION_ALREADY_ENDED", "LIMIT_REQUEST_ALREADY_PENDING",
            "CARD_ALREADY_BLOCKED", "STALE_LIMIT_VIEW", "REFUND_WINDOW_CLOSED",
            "RATE_LIMITED", "INTERNAL", "SESSION_CAPACITY",
        )
        for (code in taxonomy) {
            val mapped = ApiError.fromWireCode(code)
            assertEquals("$code must map onto its own constant", code, mapped.name)
        }
        // And nothing beyond the taxonomy hides in the enum except the fold target.
        assertEquals(taxonomy.size + 1, ApiError.entries.size)
    }

    @Test
    fun `mapping tolerates a lowercase wire code`() {
        assertEquals(ApiError.RATE_LIMITED, ApiError.fromWireCode("rate_limited"))
    }
}
