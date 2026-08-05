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
    /**
     * The last ~15 `app_event/v1` timeline entries, **oldest first**
     * (`session_create_request.v1.json`); Phase 3 always sends `[]`.
     *
     * The ordering is a contract, not a formality: the backend `RPUSH`es
     * this array into `ctx:{session_id}:events` in the order given, and
     * `ContextCompressor.render_timeline_slot` takes "the newest 15" as
     * `events[-15:]` — so a newest-first array would hand the agent the
     * OLDEST fifteen actions, rendered backwards. `EventTracker.recent()`
     * is newest-first by its own contract, so the producer
     * (`AppStateManager.sessionCreateBody`) reverses on the way in here;
     * see that function for why the reversal lives there and not in the
     * tracker.
     */
    @SerialName("recent_events") val recentEvents: List<RecentEventDto>,
)

/**
 * One `app_event/v1` timeline entry, field-for-field against
 * `protocol/schemas/app_event.v1.json` — that file is explicit about being
 * the shape authority for all three places an event appears, and
 * session-create's `recent_events` is named as one of them.
 *
 * **Audit fix (2026-08-05) — this used to be a hard `{type, name, ts}`
 * projection.** The kdoc justifying that projection claimed per-variant
 * fields were "a data-channel-only concern"; the schema's own description
 * says the opposite, and `protocol/fixtures/session_create_request.json`
 * ships the full per-variant shape in every one of its eight events. The
 * cost of the projection was concrete, not theoretical: `api_error`'s
 * [status]/[code] — the single highest-value diagnostic the pre-call
 * timeline carries, and the one fact that tells the agent *which* failure
 * the merchant just hit — was dropped on the floor, along with
 * [visible], which is the difference between a dialog the merchant is
 * staring at and one they already dismissed.
 *
 * **Flat, with nullable per-variant fields, rather than a sealed hierarchy
 * mirroring `AppEvent`'s.** `AppEvent` is a sealed interface for a stated
 * reason ("a `NAV` event without `from` should not typecheck") and that
 * reasoning is right *there* — at the point where events are constructed by
 * hand all over the app. It does not transfer here: this DTO has exactly
 * one producer (`AppStateManager.toRecentEventDto`, an exhaustive `when`
 * over that same sealed hierarchy, so the compiler already guarantees every
 * variant is mapped and mapped completely), and this file's stated job is
 * to be "a photograph of the wire". A polymorphic DTO would additionally
 * put kotlinx's class-discriminator machinery on the encode path of a body
 * that `VoiceCallService` also round-trips through a plain `Json` as an
 * Intent extra — real risk, in modules outside this one, for a type-safety
 * guarantee already held one layer up.
 *
 * **Absent, not null, is the wire contract.** Every per-variant field
 * defaults to `null` and kotlinx's `encodeDefaults` is `false` by default
 * (`NetworkModule.provideJson` does not override it), so a field left at
 * its default is *omitted from the JSON entirely* rather than serialized as
 * `null`. That is exactly what `app_event.v1.json` requires — of [value]
 * most sharply: docs/08 §2.1 has the event "omit the value rather than
 * sending [REDACTED]" for sensitive field classes, "which is why value is
 * optional here rather than required-and-nullable" (the schema's own
 * words). `VyaparApiSessionsTest` asserts the exact emitted key set per
 * variant through the production [Json], so a future `encodeDefaults = true`
 * cannot quietly start leaking `"from": null` onto every `tap`.
 */
@Serializable
public data class RecentEventDto(
    /** The closed docs/08 §2.1 taxonomy as a wire string: `nav`/`tap`/`input`/`api_error`/`dialog`. */
    @SerialName("type") val type: String,
    /**
     * Meaning depends on [type] (docs/08 §2.1's "name carries" column):
     * destination route for `nav`, target label/testTag for `tap`, field
     * testTag/label for `input`, `"METHOD path"` for `api_error`, dialog
     * title for `dialog`.
     */
    @SerialName("name") val name: String,
    /** Epoch milliseconds, on-device. */
    @SerialName("ts") val ts: Long,
    /** `nav` only, required there: the previous route. */
    @SerialName("from") val from: String? = null,
    /** `tap` only, required there: the route the tap happened on. */
    @SerialName("screen") val screen: String? = null,
    /**
     * `input` only, and **optional even there** — present for non-sensitive
     * field classes, absent otherwise (docs/07 §5 rule 6's classification).
     * The absence *is* the redaction; "the user edited the PIN field" is
     * already the whole signal.
     */
    @SerialName("value") val value: String? = null,
    /** `api_error` only, required there: the HTTP status code, e.g. `402`. */
    @SerialName("status") val status: Int? = null,
    /** `api_error` only, required there: the docs/13 §1.1 error-code vocabulary string. */
    @SerialName("code") val code: String? = null,
    /** `dialog` only, required there: `true` on attach, `false` on detach. */
    @SerialName("visible") val visible: Boolean? = null,
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
