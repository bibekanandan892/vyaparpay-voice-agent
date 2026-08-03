package com.vyaparpay.voice.signaling

import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.decodeFromJsonElement
import kotlinx.serialization.json.encodeToJsonElement
import kotlinx.serialization.json.jsonObject

/**
 * The one place signaling JSON is encoded and decoded — the Kotlin analogue of
 * the backend's envelope handling in `SignalingServer._run_message_loop`
 * (backend/app/voice/signaling.py).
 *
 * Decode policy, in the backend's own order:
 * - malformed JSON / bad envelope shape → [Decoded.Ignored] (the client-side
 *   twist on the server's `error` reply: a client must never kill or spam a
 *   live control plane over one garbled frame),
 * - envelope `v` ≠ 1 → [Decoded.Ignored] (a `/v2` server would bump REST and
 *   schema in lockstep, docs/13 §9 — a stray v2 frame is noise, not a signal),
 * - unknown `type` → [Decoded.Ignored] — docs/13 §9 makes this mandatory, not
 *   defensive style: a future additive type must not error,
 * - per-type payload that fails its typed decode → [Decoded.Ignored], except
 *   `bye`, whose payload DTO is total (see [ByePayloadDto]) precisely so a
 *   hang-up can never be dropped on a technicality.
 */
internal object SignalWire {

    internal val json: Json = Json {
        // docs/13 §9's client rule: ignore unknown fields, everywhere.
        ignoreUnknownKeys = true
        // Defaulted fields (envelope payload, ice sdpMid/sdpMLineIndex) must
        // still reach the wire — the backend's Pydantic models always emit
        // every key, and mirroring that keeps the §6 examples byte-shaped.
        encodeDefaults = true
    }

    /** One decoded inbound frame, routed by [OkHttpSignalingClient]. */
    internal sealed interface Decoded {
        /** A frame the call logic consumes, surfaced on [SignalingClient.frames]. */
        data class Frame(val frame: SignalFrame) : Decoded

        /** A server `ping` — answered with a `pong` echoing `ts`, never surfaced. */
        data class PingFromServer(val ts: Long) : Decoded

        /** A `pong` for the keepalive loop — consumed internally, never surfaced. */
        data class Pong(val ts: Long) : Decoded

        /** Anything the client must silently drop (docs/13 §9). */
        data class Ignored(val why: String) : Decoded
    }

    internal fun encode(frame: OutboundSignal): String {
        val (type, payload) = when (frame) {
            is OutboundSignal.Offer ->
                SIG_TYPE_OFFER to json.encodeToJsonElement(SdpPayloadDto(frame.sdp))
            is OutboundSignal.LocalIce ->
                SIG_TYPE_ICE to json.encodeToJsonElement(
                    IcePayloadDto(frame.candidate, frame.sdpMid, frame.sdpMLineIndex),
                )
            is OutboundSignal.Bye ->
                SIG_TYPE_BYE to json.encodeToJsonElement(ByePayloadDto(frame.reason))
        }
        return json.encodeToString(
            SignalEnvelopeDto.serializer(),
            SignalEnvelopeDto(v = WIRE_ENVELOPE_V, type = type, payload = payload.jsonObject),
        )
    }

    internal fun encodePing(ts: Long): String = encodeKeepalive(SIG_TYPE_PING, ts)

    internal fun encodePong(ts: Long): String = encodeKeepalive(SIG_TYPE_PONG, ts)

    private fun encodeKeepalive(type: String, ts: Long): String =
        json.encodeToString(
            SignalEnvelopeDto.serializer(),
            SignalEnvelopeDto(
                v = WIRE_ENVELOPE_V,
                type = type,
                payload = json.encodeToJsonElement(PingPayloadDto(ts)).jsonObject,
            ),
        )

    internal fun decode(text: String): Decoded {
        val envelope = try {
            json.decodeFromString(SignalEnvelopeDto.serializer(), text)
        } catch (_: SerializationException) {
            return Decoded.Ignored("malformed envelope")
        } catch (_: IllegalArgumentException) {
            return Decoded.Ignored("malformed envelope")
        }
        if (envelope.v != WIRE_ENVELOPE_V) {
            return Decoded.Ignored("unsupported envelope version ${envelope.v}")
        }
        return when (envelope.type) {
            SIG_TYPE_ANSWER -> decodePayload<SdpPayloadDto>(envelope) {
                SignalFrame.Answer(it.sdp)
            }
            SIG_TYPE_ICE -> decodePayload<IcePayloadDto>(envelope) {
                SignalFrame.RemoteIce(it.candidate, it.sdpMid, it.sdpMLineIndex)
            }
            SIG_TYPE_BYE -> decodePayload<ByePayloadDto>(envelope) {
                SignalFrame.Bye(it.reason)
            }
            SIG_TYPE_ERROR -> decodePayload<ErrorPayloadDto>(envelope) {
                SignalFrame.ServerError(it.code, it.message)
            }
            SIG_TYPE_PING -> decodeKeepalive(envelope) { Decoded.PingFromServer(it) }
            SIG_TYPE_PONG -> decodeKeepalive(envelope) { Decoded.Pong(it) }
            // docs/13 §9: unknown types MUST be ignored.
            else -> Decoded.Ignored("unknown type ${envelope.type}")
        }
    }

    private inline fun <reified T> decodePayload(
        envelope: SignalEnvelopeDto,
        map: (T) -> SignalFrame,
    ): Decoded = try {
        Decoded.Frame(map(json.decodeFromJsonElement(envelope.payload)))
    } catch (_: SerializationException) {
        Decoded.Ignored("malformed ${envelope.type} payload")
    } catch (_: IllegalArgumentException) {
        Decoded.Ignored("malformed ${envelope.type} payload")
    }

    private inline fun decodeKeepalive(
        envelope: SignalEnvelopeDto,
        map: (Long) -> Decoded,
    ): Decoded = try {
        map(json.decodeFromJsonElement<PingPayloadDto>(envelope.payload).ts)
    } catch (_: SerializationException) {
        Decoded.Ignored("malformed ${envelope.type} payload")
    } catch (_: IllegalArgumentException) {
        Decoded.Ignored("malformed ${envelope.type} payload")
    }
}
