package com.vyaparpay.voice.signaling

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject

/**
 * Wire DTOs for the signaling WebSocket protocol (docs/13 §6), mirrored
 * field-for-field against the backend's `SignalMessage`
 * (backend/app/domain/voice.py) and the §6 examples.
 *
 * Conventions, matching :core:network's DTO discipline:
 * - Every property carries an explicit [SerialName] so a Kotlin rename can
 *   never silently change the wire.
 * - [SignalEnvelopeDto.type] is an open `String`, not an enum — docs/13 §9
 *   makes ignoring unknown envelope types a mandatory client rule, and an enum
 *   would turn a future additive type into a decode crash.
 * - [SignalEnvelopeDto.payload] stays an uninterpreted [JsonObject]; the typed
 *   payload DTOs below are applied per-type by [SignalWire], so one malformed
 *   payload never poisons envelope-level decoding.
 */

/** The envelope version this client speaks (docs/13 §6, §9). */
public const val WIRE_ENVELOPE_V: Int = 1

public const val SIG_TYPE_OFFER: String = "offer"
public const val SIG_TYPE_ANSWER: String = "answer"
public const val SIG_TYPE_ICE: String = "ice"
public const val SIG_TYPE_BYE: String = "bye"
public const val SIG_TYPE_ERROR: String = "error"
public const val SIG_TYPE_PING: String = "ping"
public const val SIG_TYPE_PONG: String = "pong"

/** `{"v": 1, "type": "...", "payload": {...}}` — every signaling frame's shape (docs/13 §6). */
@Serializable
public data class SignalEnvelopeDto(
    @SerialName("v") val v: Int,
    @SerialName("type") val type: String,
    @SerialName("payload") val payload: JsonObject = JsonObject(emptyMap()),
)

/** `offer` / `answer` payload: `{sdp}` (docs/13 §6). */
@Serializable
public data class SdpPayloadDto(
    @SerialName("sdp") val sdp: String,
)

/**
 * `ice` payload: `{candidate, sdpMid, sdpMLineIndex}` (docs/13 §6).
 *
 * [candidate] is deliberately nullable **without** a default: `null` is the
 * end-of-candidates marker the server sends after its answer
 * (backend/app/voice/peer_session.py judgment call 3), so the key must always
 * be present on the wire — encoded as an explicit `null`, never omitted — and
 * a frame missing it entirely is malformed, exactly as the server itself
 * validates the client's ice frames.
 */
@Serializable
public data class IcePayloadDto(
    @SerialName("candidate") val candidate: String?,
    @SerialName("sdpMid") val sdpMid: String? = null,
    @SerialName("sdpMLineIndex") val sdpMLineIndex: Int? = null,
)

/**
 * `bye` payload: `{reason}` — `user_hangup`, `agent_hangup`, or `error`
 * (docs/13 §6). [reason] defaults rather than requires: a `bye` whose payload
 * fails strict decoding must still end the call — missing the hang-up because
 * of a junk field would leave a live mic on a dead session.
 */
@Serializable
public data class ByePayloadDto(
    @SerialName("reason") val reason: String = "unknown",
)

/**
 * `error` payload: `{code, message}` where `code` is the docs/13 §1.1
 * vocabulary — the same strings as REST, mapped client-side via
 * `ApiError.fromWireCode` (unknown members fold to `UNKNOWN` per §9).
 */
@Serializable
public data class ErrorPayloadDto(
    @SerialName("code") val code: String,
    @SerialName("message") val message: String = "",
)

/** `ping` / `pong` payload: `{ts}` epoch milliseconds (docs/13 §6). */
@Serializable
public data class PingPayloadDto(
    @SerialName("ts") val ts: Long,
)
