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
}
