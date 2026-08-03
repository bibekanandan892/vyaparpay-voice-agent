package com.vyaparpay.core.network

/**
 * The client-side mirror of the docs/13-api-contracts.md §1.1 error taxonomy,
 * one constant per wire code, in the table's own order.
 *
 * [UNKNOWN] is not a gap in the enum — it is the contract. docs/13 §9 makes
 * forward compatibility the client's job: a server that grows a new error code
 * within an existing prefix class must not crash an older app, so an
 * unrecognised code folds here instead of throwing.
 */
public enum class ApiError {
    // 400
    VALIDATION_SCHEMA,
    VALIDATION_UNSUPPORTED_VERSION,

    // 401
    AUTH_MISSING_TOKEN,
    AUTH_INVALID_TOKEN,
    AUTH_EXPIRED_TOKEN,

    // 402
    DAILY_LIMIT_EXCEEDED,
    INSUFFICIENT_BALANCE,

    // 404
    SESSION_NOT_FOUND,
    RESOURCE_NOT_FOUND,
    SESSION_SUMMARY_PENDING,

    // 409
    SESSION_ALREADY_ENDED,
    LIMIT_REQUEST_ALREADY_PENDING,
    CARD_ALREADY_BLOCKED,
    STALE_LIMIT_VIEW,
    REFUND_WINDOW_CLOSED,

    // 429
    RATE_LIMITED,

    // 500
    INTERNAL,

    // 503
    SESSION_CAPACITY,

    /** Any code this build does not recognise — including transport-level failures that never produced a wire code at all. */
    UNKNOWN,
    ;

    public companion object {
        private val byWireCode: Map<String, ApiError> =
            entries.filterNot { it == UNKNOWN }.associateBy { it.name }

        /** Maps an envelope's `error.code` onto the enum, folding anything unrecognised to [UNKNOWN]. */
        public fun fromWireCode(code: String?): ApiError =
            byWireCode[code?.uppercase()] ?: UNKNOWN
    }
}
