package com.vyaparpay.core.network

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject

/**
 * Wire DTOs for the four docs/13 §2 session endpoints, field-for-field against
 * `protocol/schemas/session_create_request.v1.json` and
 * `session_create_response.v1.json`.
 *
 * Conventions, applied uniformly:
 * - Every property carries an explicit [SerialName] so a Kotlin rename can
 *   never silently change the wire.
 * - Timestamps stay ISO-8601 strings — the DTO layer is a photograph of the
 *   wire, and parsing is the consumer's decision, not this module's.
 * - Bearer material ([ConnectBundleDto.signalingToken], [IceServerDto.credential])
 *   is masked in `toString()`, mirroring the backend's `repr=False` on the
 *   same fields (backend/app/api/routes/sessions.py): an accidental log of a
 *   whole bundle must be inert.
 */

/** What a masked secret renders as in `toString()`. */
private const val REDACTED: String = "<redacted>"

/**
 * `POST /v1/sessions` request body (docs/13 §2.1).
 *
 * The schema pins `additionalProperties: false` — new request fields are a
 * server-contract change, never a client improvisation — so this DTO carries
 * exactly the schema's three required keys and all three are always encoded,
 * `null`s included.
 */
@Serializable
public data class SessionCreateRequestDto(
    /** Must equal the JWT `sub` claim — the server verifies and rejects a mismatch (docs/13 §1.2). */
    @SerialName("user_id") val userId: String,
    /**
     * Full `screen_context/v1` IR, or `null` when the client has no capture.
     * Phase 3 always sends `null`; the typed IR shape lands with Phase-4
     * context capture (schema authority: docs/07), so an uninterpreted
     * [JsonObject] is the honest carrier until then.
     */
    @SerialName("screen_context") val screenContext: JsonObject?,
    /** The last ~15 `app_event/v1` timeline entries, oldest first; Phase 3 always sends `[]`. */
    @SerialName("recent_events") val recentEvents: List<RecentEventDto>,
)

/**
 * One `app_event/v1` timeline entry — the three fields every event variant
 * carries (the schema's own scope). Per-variant fields are docs/07/08
 * territory and arrive with Phase-4 capture.
 */
@Serializable
public data class RecentEventDto(
    @SerialName("type") val type: String,
    @SerialName("name") val name: String,
    /** Epoch milliseconds, on-device. */
    @SerialName("ts") val ts: Long,
)

/**
 * The connect bundle — `data` of the `201` from `POST /v1/sessions`, and of
 * the re-mint `POST /v1/sessions/{id}/token` (docs/13 §2.1, §2.2).
 */
@Serializable
public data class ConnectBundleDto(
    @SerialName("session_id") val sessionId: String,
    /** `wss://` URL of the voice-worker SignalingServer (docs/13 §6). */
    @SerialName("signaling_url") val signalingUrl: String,
    /**
     * Opaque one-time token (`st_…`), 5-min TTL, verified and burned at WS
     * accept (docs/13 §6.2). Live bearer material: masked in [toString].
     */
    @SerialName("signaling_token") val signalingToken: String,
    /** Passed verbatim to `PeerConnection` construction — STUN entry first, then TURN (docs/13 §2.1, §7). */
    @SerialName("ice_servers") val iceServers: List<IceServerDto>,
    /** ISO-8601 connect deadline of the signaling token — NOT the session's lifetime (docs/13 §6.2). */
    @SerialName("expires") val expires: String,
) {
    override fun toString(): String =
        "ConnectBundleDto(sessionId=$sessionId, signalingUrl=$signalingUrl, " +
            "signalingToken=$REDACTED, iceServers=$iceServers, expires=$expires)"
}

/**
 * One RTCIceServer entry. The STUN entry carries only [urls]; the TURN entry
 * adds the coturn `use-auth-secret` credential pair (docs/13 §7). Absent keys
 * decode to `null` — the backend *omits* them on STUN rather than nulling them.
 */
@Serializable
public data class IceServerDto(
    /** `stun:`/`turn:`/`turns:` URIs; for TURN, udp on 3478 first, tls on 5349 fallback. */
    @SerialName("urls") val urls: List<String>,
    /**
     * TURN REST-API username `<expiry_unix_ts>:<session_id>`. Deliberately
     * *not* masked: it is the attributable-in-logs half of the pair (docs/13 §7).
     */
    @SerialName("username") val username: String? = null,
    /** `base64(HMAC-SHA1(TURN_SECRET, username))` — live credential, masked in [toString]. */
    @SerialName("credential") val credential: String? = null,
) {
    override fun toString(): String =
        "IceServerDto(urls=$urls, username=$username, " +
            "credential=${if (credential == null) null else REDACTED})"
}

/**
 * `data` of `DELETE /v1/sessions/{id}` (docs/13 §2.2). Idempotent by
 * contract: a repeat call returns this same body, never a 409.
 */
@Serializable
public data class SessionEndDto(
    @SerialName("session_id") val sessionId: String,
    /** The terminal state the hang-up commits the call to — `"ended"`. Kept a string for docs/13 §9 forward compatibility. */
    @SerialName("state") val state: String,
)

/**
 * `data` of `GET /v1/sessions/{id}/summary` (docs/13 §2.3). Until the
 * post-call pipeline lands the server answers `404 SESSION_SUMMARY_PENDING`
 * instead — that arrives here as an [ApiResult.Failure], not a DTO.
 */
@Serializable
public data class SessionSummaryDto(
    @SerialName("session_id") val sessionId: String,
    /** ISO-8601 call start. */
    @SerialName("started_at") val startedAt: String,
    @SerialName("duration_s") val durationSeconds: Int,
    @SerialName("turn_count") val turnCount: Int,
    /** The summary card's prose — the record of the call (docs/12 §4.2: the transcript is never persisted). */
    @SerialName("summary") val summary: String,
    /** Present-and-null when the call reached no resolution, so clients can branch on it. */
    @SerialName("resolution") val resolution: SessionResolutionDto? = null,
    @SerialName("actions") val actions: List<SessionActionDto>,
)

/** The structured outcome inside a session summary, e.g. a submitted limit increase (docs/13 §2.3). */
@Serializable
public data class SessionResolutionDto(
    @SerialName("type") val type: String,
    @SerialName("reference") val reference: String,
    @SerialName("eta_hours") val etaHours: Int? = null,
)

/** One tool the agent ran during the call, in first-use order (docs/13 §2.3). */
@Serializable
public data class SessionActionDto(
    @SerialName("tool") val tool: String,
    @SerialName("status") val status: String,
)
