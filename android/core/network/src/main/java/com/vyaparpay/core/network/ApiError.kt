package com.vyaparpay.core.network

/**
 * The client-side mirror of the docs/13-api-contracts.md §1.1 error taxonomy.
 *
 * [UNKNOWN] is not a gap in the enum — it is the contract. docs/13 §8 makes
 * forward compatibility the client's job: a server that grows a new error code
 * must not crash an older app, so an unrecognised code folds here instead of
 * throwing.
 */
public enum class ApiError {
    DAILY_LIMIT_EXCEEDED,
    INSUFFICIENT_BALANCE,
    RATE_LIMITED,
    SESSION_CAPACITY,
    AUTH_EXPIRED_TOKEN,
    VALIDATION_SCHEMA,
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
