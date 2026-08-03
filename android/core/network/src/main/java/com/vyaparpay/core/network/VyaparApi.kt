package com.vyaparpay.core.network

import retrofit2.http.Body
import retrofit2.http.DELETE
import retrofit2.http.GET
import retrofit2.http.POST
import retrofit2.http.Path

/**
 * The typed `/v1` surface (docs/03 §1 module table: `VyaparApi`) — currently
 * the four session-lifecycle endpoints of docs/13 §2. Business endpoints are
 * additive here as their features land (docs/13 §9).
 *
 * Paths are relative to a base URL that **already carries `/v1`**
 * (docs/13 §1: "the Retrofit base URL, which already carries /v1") — which is
 * also why the timeline's `api_error` entries log the short form
 * (`POST /sessions`, `POST /payments`).
 *
 * Every method returns [ApiResult] and never throws wire trouble: the
 * [EnvelopeAdapter] folds non-2xx envelopes, malformed bodies, and transport
 * failures into [ApiResult.Failure].
 */
public interface VyaparApi {

    /**
     * Mint a call session (docs/13 §2.1): ingests the initial context and
     * returns the connect bundle — session id, signaling URL + one-time
     * token, ICE servers, connect deadline. `201` on success; the failures
     * worth branching on are [ApiError.RATE_LIMITED] and
     * [ApiError.SESSION_CAPACITY] ("couldn't start the call" — nothing to
     * tear down, docs/03 §3.2).
     */
    @POST("sessions")
    public suspend fun createSession(
        @Body body: SessionCreateRequestDto,
    ): ApiResult<ConnectBundleDto>

    /**
     * Re-mint the signaling token and TURN credentials for a live session
     * (docs/13 §2.2) — the reconnect path after a network change killed the
     * WebSocket. Same bundle shape as [createSession];
     * [ApiError.SESSION_ALREADY_ENDED] when there is nothing left to
     * connect to.
     */
    @POST("sessions/{session_id}/token")
    public suspend fun remintSessionToken(
        @Path("session_id") sessionId: String,
    ): ApiResult<ConnectBundleDto>

    /**
     * Explicit hang-up (docs/13 §2.2). Idempotent by contract: a repeat call
     * answers the same body, never a 409.
     */
    @DELETE("sessions/{session_id}")
    public suspend fun endSession(
        @Path("session_id") sessionId: String,
    ): ApiResult<SessionEndDto>

    /**
     * The post-call summary card (docs/13 §2.3). Until the post-call
     * pipeline lands this answers [ApiError.SESSION_SUMMARY_PENDING] (with
     * `Retry-After: 2` on the wire) and the client polls once or twice.
     */
    @GET("sessions/{session_id}/summary")
    public suspend fun getSessionSummary(
        @Path("session_id") sessionId: String,
    ): ApiResult<SessionSummaryDto>
}
